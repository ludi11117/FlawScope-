import { defineConfig, loadEnv } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [react()],
    server: {
      port: 5173,
      // 开发期把 /api 代理到后端，绕开 CORS。
      // 生产环境由 nginx 承担同样的角色（见 web/nginx.conf），
      // 这样前端代码里的请求路径在两种环境下完全一致，不需要判断环境。
      proxy: {
        '/api': {
          target: apiTarget,
          changeOrigin: true,
          rewrite: (p) => p.replace(/^\/api/, ''),
          // SSE 必须关闭代理缓冲：http-proxy 默认会缓冲响应体，
          // 不关的话前端的"逐节点进度"会退化成"最后一次性全出来"。
          configure: (proxy) => {
            proxy.on('proxyRes', (proxyRes) => {
              proxyRes.headers['x-accel-buffering'] = 'no'
              proxyRes.headers['cache-control'] = 'no-cache'
            })
          },
        },
      },
    },
    build: {
      outDir: 'dist',
      // 不产出 sourcemap：nginx 把 /assets/ 公开托管，`sourcemap: true` 等于
      // 把完整的 TypeScript 源码（含注释里的设计取舍与内部端点）一并发布出去。
      // 需要调试线上问题时，本地 `npm run build` 出带 map 的产物即可。
      sourcemap: false,
    },
  }
})
