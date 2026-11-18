import os
import sys
import subprocess
import multiprocessing as mp
import shutil
import tarfile
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed

# ── 自动选择空闲 GPU ─────────────────────────────
# 在 import torch 之前查询 nvidia-smi，选显存最多的 GPU，
# 避免撞上已被其他进程占满的卡。
_CUDA_BEST_GPU: int | None = None
try:
    _result = subprocess.run(
        ["nvidia-smi", "--query-gpu=index,memory.free", "--format=csv,noheader,nounits"],
        capture_output=True, text=True, timeout=10,
    )
    if _result.returncode == 0:
        _gpus = []
        for _line in _result.stdout.strip().split("\n"):
            _parts = _line.strip().split(", ")
            if len(_parts) == 2:
                _gpus.append((int(_parts[0]), int(_parts[1])))
        if _gpus:
            # 按空闲内存降序排列
            _gpus.sort(key=lambda x: x[1], reverse=True)
            _best = _gpus[0]
            _CUDA_BEST_GPU = _best[0]
            # 只暴露空闲最多的那张卡，避免 Marker 误初始化到被占的卡
            os.environ["CUDA_VISIBLE_DEVICES"] = str(_best[0])
            print(f"  Selected GPU {_best[0]} (free: {_best[1]} MiB)")
except Exception:
    pass

import torch

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

# Import Marker only after environment variables are ready.
from marker.converters.pdf import PdfConverter
from marker.models import create_model_dict


def test_gpu_availability() -> bool:
    """Print basic GPU info and return whether CUDA is available."""
    print("Checking GPU status...")
    try:
        print(f"  PyTorch version: {torch.__version__}")
        print(f"  CUDA available: {torch.cuda.is_available()}")
        if torch.cuda.is_available():
            print(f"  Visible GPU count: {torch.cuda.device_count()}")
            for i in range(torch.cuda.device_count()):
                props = torch.cuda.get_device_properties(i)
                print(f"  GPU{i}: {props.name}, {props.total_memory / 1024**3:.1f} GB (CUDA_VISIBLE_DEVICES={os.environ.get('CUDA_VISIBLE_DEVICES', '(not set)')})")
            return True

        print("  CUDA not available, fallback to CPU")
        return False
    except Exception as exc:
        print(f"  GPU check failed: {exc}")
        return False


def convert_single_pdf(args):
    """Convert one PDF to markdown in a worker process."""
    pdf_path, output_path, device_id, export_images = args

    try:
        # CUDA_VISIBLE_DEVICES 已在顶层设好，torch 只看到空闲最多的那张卡
        device = "cuda" if torch.cuda.is_available() else "cpu"

        model_dict = create_model_dict(device=device)
        converter = PdfConverter(artifact_dict=model_dict)
        rendered = converter(pdf_path)

        with open(output_path, "w", encoding="utf-8") as handle:
            handle.write(rendered.markdown)

        exported_images = 0
        image_tar_path = None
        if export_images and hasattr(rendered, "images") and rendered.images:
            output_dir = os.path.dirname(output_path)
            pdf_stem = Path(pdf_path).stem
            image_dir = os.path.join(output_dir, pdf_stem)
            os.makedirs(image_dir, exist_ok=True)

            for image_name, image_data in rendered.images.items():
                safe_image_name = os.path.basename(str(image_name))
                image_path = os.path.join(image_dir, safe_image_name)

                if isinstance(image_data, bytes):
                    with open(image_path, "wb") as image_handle:
                        image_handle.write(image_data)
                elif hasattr(image_data, "save"):
                    image_data.save(image_path)
                else:
                    raise TypeError(f"Unsupported image type: {type(image_data)}")

                exported_images += 1

            image_tar_path = os.path.join(output_dir, f"{pdf_stem}.tar")
            with tarfile.open(image_tar_path, "w") as tar_handle:
                tar_handle.add(image_dir, arcname=os.path.basename(image_dir))

            shutil.rmtree(image_dir)

        return {
            "success": True,
            "pdf_path": pdf_path,
            "output_path": output_path,
            "length": len(rendered.markdown),
            "images": len(rendered.images) if hasattr(rendered, "images") else 0,
            "exported_images": exported_images,
            "image_tar_path": image_tar_path,
        }
    except Exception as exc:
        return {
            "success": False,
            "pdf_path": pdf_path,
            "error": str(exc),
        }


def batch_convert_parallel(
    folder_path,
    output_folder=None,
    num_workers=None,
    export_images=False,
    allowlist=None,
) -> tuple[int, int]:
    """Convert all PDFs in a folder in parallel.

    Returns:
        (success_count, total_count)
    """
    if output_folder is None:
        output_folder = folder_path + "_markdown"

    os.makedirs(output_folder, exist_ok=True)

    pdf_files = [name for name in os.listdir(folder_path) if name.lower().endswith(".pdf")]
    if allowlist is not None:
        allowlist_lower = {name.lower() for name in allowlist}
        pdf_files = [name for name in pdf_files if name.lower() in allowlist_lower]
    if not pdf_files:
        print(f"No PDF files found in: {folder_path}")
        return 0, 0

    print(f"\nFound {len(pdf_files)} PDF files")

    has_gpu = test_gpu_availability()
    num_gpus = torch.cuda.device_count() if has_gpu else 0

    if num_workers is None:
        if has_gpu:
            # Keep a conservative worker count to avoid GPU OOM.
            num_workers = min(num_gpus * 2, len(pdf_files), 8)
        else:
            num_workers = min(os.cpu_count() or 4, len(pdf_files), 4)

    if num_workers < 1:
        num_workers = 1
    num_workers = min(num_workers, 3)

    print(f"Worker count: {num_workers}")
    if has_gpu:
        print(f"Detected GPU count: {num_gpus}")
        print("Tip: each worker may use about 3.5-5 GB GPU memory")

    tasks = []
    for index, pdf_file in enumerate(pdf_files):
        pdf_path = os.path.join(folder_path, pdf_file)
        output_path = os.path.join(output_folder, pdf_file.replace(".pdf", ".md"))
        device_id = index % num_gpus if num_gpus > 0 else None
        tasks.append((pdf_path, output_path, device_id, export_images))

    print("\nStarting parallel conversion...")
    success_count = 0

    if num_workers <= 1:
        # Single-worker mode avoids multiprocessing overhead and CUDA fork issues.
        for index, task in enumerate(tasks, 1):
            result = convert_single_pdf(task)
            name = Path(result["pdf_path"]).name
            if result["success"]:
                success_count += 1
                print(f"  OK [{index}/{len(pdf_files)}] {name} -> {result['length']} chars")
                if export_images and result.get("image_tar_path"):
                    print(
                        f"     images: {result.get('exported_images', 0)} files -> {result['image_tar_path']}"
                    )
            else:
                print(f"  FAIL [{index}/{len(pdf_files)}] {name}: {result.get('error', 'unknown error')}")
    else:
        # CUDA requires spawn start method on Linux multiprocessing.
        spawn_context = mp.get_context("spawn")
        with ProcessPoolExecutor(max_workers=num_workers, mp_context=spawn_context) as executor:
            futures = {executor.submit(convert_single_pdf, task): task for task in tasks}
            for index, future in enumerate(as_completed(futures), 1):
                result = future.result()
                name = Path(result["pdf_path"]).name
                if result["success"]:
                    success_count += 1
                    print(f"  OK [{index}/{len(pdf_files)}] {name} -> {result['length']} chars")
                    if export_images and result.get("image_tar_path"):
                        print(
                            f"     images: {result.get('exported_images', 0)} files -> {result['image_tar_path']}"
                        )
                else:
                    print(f"  FAIL [{index}/{len(pdf_files)}] {name}: {result.get('error', 'unknown error')}")

    print(f"\nBatch finished: {success_count}/{len(pdf_files)} succeeded")
    return success_count, len(pdf_files)


def main() -> None:
    """CLI entry point."""
    print("=" * 60)
    print("Marker PDF Converter (Batch Mode)")
    print("=" * 60)

    if len(sys.argv) < 2:
        print("\nUsage:")
        print(
            "  python convert.py --batch <pdf_folder_path> "
            "[--output <output_folder_path>] [--workers N] [--files a.pdf,b.pdf] [--export-images]"
        )
        print("\nExamples:")
        print("  python convert.py --batch /path/to/pdf/folder")
        print("  python convert.py --batch /path/to/pdf/folder --output /path/to/output")
        print("  python convert.py --batch /path/to/pdf/folder --workers 2")
        print("  python convert.py --batch /path/to/pdf/folder --files a.pdf,b.pdf")
        print("  python convert.py --batch /path/to/pdf/folder --output /path/to/output --export-images")
        sys.exit(1)

    if sys.argv[1] != "--batch":
        print("This version only supports batch mode. Use --batch.")
        sys.exit(1)

    if len(sys.argv) < 3:
        print("Please provide a PDF folder path after --batch")
        sys.exit(1)

    folder_path = sys.argv[2]
    output_folder = None
    num_workers = None
    export_images = "--export-images" in sys.argv
    allowlist = None

    if "--output" in sys.argv:
        idx = sys.argv.index("--output")
        if idx + 1 < len(sys.argv):
            output_folder = sys.argv[idx + 1]
        else:
            print("--output requires a path")
            sys.exit(1)

    if "--workers" in sys.argv:
        idx = sys.argv.index("--workers")
        if idx + 1 < len(sys.argv):
            try:
                num_workers = int(sys.argv[idx + 1])
            except ValueError:
                print("--workers must be an integer")
                sys.exit(1)

    if "--files" in sys.argv:
        idx = sys.argv.index("--files")
        if idx + 1 < len(sys.argv):
            raw_files = sys.argv[idx + 1]
            allowlist = {os.path.basename(name.strip()) for name in raw_files.split(",") if name.strip()}
            if not allowlist:
                print("--files must include at least one filename")
                sys.exit(1)
        else:
            print("--files requires a comma-separated list")
            sys.exit(1)

    success_count, total_count = batch_convert_parallel(
        folder_path,
        output_folder=output_folder,
        num_workers=num_workers,
        export_images=export_images,
        allowlist=allowlist,
    )

    if total_count == 0:
        sys.exit(1)
    if success_count < total_count:
        sys.exit(2)


if __name__ == "__main__":
    main()