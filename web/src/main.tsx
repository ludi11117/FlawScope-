import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { App } from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './styles/global.css'

const root = document.getElementById('root')
if (!root) throw new Error('未找到 #root 挂载点')

// ErrorBoundary 必须在最外层：React 渲染期抛异常会卸载整棵树，
// 没有它的话任何一处渲染错误（例如把后端的 422 detail 数组当字符串渲染）
// 都会变成一片白屏，用户连"出错了"都看不到。
createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
)
