const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '../../../..');
const envFile = path.join(root, '.envs');
if (fs.existsSync(envFile)) {
  if (typeof process.loadEnvFile !== 'function') {
    throw new Error('读取 .envs 需要 Node.js 22 或更高版本');
  }
  process.loadEnvFile(envFile);
}

function requireEnv(name) {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`请在 .envs 配置 ${name}`);
  return value;
}

module.exports = { root, requireEnv };
