import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { createHash } from 'node:crypto'
import { readdirSync, readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const root = path.dirname(fileURLToPath(import.meta.url))
const sha256 = (value: string | Uint8Array) => createHash('sha256').update(value).digest('hex')
function inputFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap(entry => {
    const target = path.join(directory, entry.name)
    return entry.isDirectory() ? inputFiles(target) : [target]
  })
}

export default defineConfig({
  plugins: [vue(), {
    name: 'purrai-build-identity', enforce: 'post',
    generateBundle(_options, bundle) {
      const inputs = [...inputFiles(path.join(root, 'src')), ...['index.html', 'package.json', 'package-lock.json', 'tsconfig.json', 'vite.config.ts'].map(name => path.join(root, name))]
      this.emitFile({ type: 'asset', fileName: 'build-manifest.json', source: JSON.stringify({
        schema: 1,
        inputs: Object.fromEntries(inputs.sort().map(file => [path.relative(root, file).split(path.sep).join('/'), sha256(readFileSync(file))])),
        outputs: Object.fromEntries(Object.values(bundle).map(file => [file.fileName, sha256(file.type === 'chunk' ? file.code : file.source)])),
      }) })
    },
  }],
  server: { proxy: { '/api': 'http://127.0.0.1:8765', '/auth': 'http://127.0.0.1:8765' } },
  build: { chunkSizeWarningLimit: 1200, emptyOutDir: false },
})
