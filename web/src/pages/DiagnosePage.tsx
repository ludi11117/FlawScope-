/**
 * 诊断页 —— 本次 React 改造的切入页面。
 *
 * 覆盖 Streamlit 版诊断页的全部交互：文本输入、图片上传、多轮追问、
 * 流式进度、结果展示、工单下载。历史页与统计页保留在 Streamlit，
 * 待验证这条链路稳定后再迁（避免一次替换太多导致回归面失控）。
 */

import { useCallback, useRef, useState, type CSSProperties } from 'react'
import type { DiagnosisStreamApi } from '../hooks/useDiagnosisStream'
import { useHealth } from '../hooks/useHealth'
import { StateMachineView } from '../components/StateMachineView'
import { ResultView } from '../components/ResultView'
import { workorderUrl } from '../api/client'
import { readyWarnings, startupHint } from '../api/health'
import { btnStyle } from '../ui/button'
import { MAX_PAYLOAD_CHARS } from '../hooks/diagnosisContext'

const MAX_IMAGE_BYTES = 4 * 1024 * 1024

/**
 * 仅供读屏软件的标签样式。
 * 输入框必须有可访问名，但视觉上这里是"大卡片里一段没有边框的文字"，
 * 加一个可见的 label 会破坏版式，所以用视觉隐藏而不是省略标签。
 */
const SR_ONLY: CSSProperties = {
  position: 'absolute',
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
  border: 0,
}

/** 示例故障描述：新用户不知道该输入什么，直接给几条可点的样板 */
const SAMPLES: { label: string; text: string }[] = [
  {
    label: '主轴异响发热',
    text: '那台数控机床主轴转起来一顿一顿的，还有怪声，温度也高得离谱，摸着烫手',
  },
  {
    label: '液压压力不足',
    text: '液压站压力上不去，只有额定值的一半，动作明显没劲，油温偏高',
  },
  {
    label: '空压机排气温度高',
    text: '空压机运行半小时就报排气温度过高跳机，冷却器摸着挺烫的',
  },
  {
    label: '设备不匹配（应降级）',
    text: '我们食堂的洗碗机最近老是漏水，帮我看看什么原因',
  },
]

export function DiagnosePage({ diagnosis }: { diagnosis: DiagnosisStreamApi }) {
  // 诊断状态由 App 持有并透传：切到历史/统计页时本组件会被卸载，
  // 若状态随组件走，跑了一半的诊断与已拿到的结果都会丢。
  const { state, start, abort, reset } = diagnosis
  // 这里用的是与顶栏**同一个 hook 的独立实例**：组件各持一份状态，
  // 但两者探的是同一个端点、间隔规则一致，不会出现"顶栏说好了、按钮还灰着"。
  // 没有提到 App 用 context 下发，是因为只有两个消费点，
  // 引入 context 的复杂度大于收益。
  const health = useHealth()
  const [input, setInput] = useState('')
  const [imagePreview, setImagePreview] = useState<string | null>(null)
  const [imageBase64, setImageBase64] = useState<string>('')
  const fileRef = useRef<HTMLInputElement>(null)

  // 后端未就绪时不允许发起诊断。否则会白等十几秒再拿到一个失败，
  // 而失败原因（服务还没起来）用户从错误信息里读不出来。
  const notReady = health.state === 'starting'
  const backendDown = health.state === 'down'
  const blocked = notReady || backendDown

  const onPickImage = useCallback(async (file: File | undefined) => {
    if (!file) return
    if (file.size > MAX_IMAGE_BYTES) {
      alert(`图片过大（${(file.size / 1024 / 1024).toFixed(1)}MB），请压缩到 4MB 以内`)
      return
    }
    // 前端先压缩再传：后端 MAX_IMAGE_BASE64_CHARS 有上限，
    // 原图直传很容易在 4MB 文件上就超限（base64 会膨胀约 33%）
    const compressed = await compressImage(file)
    setImagePreview(compressed.dataUrl)
    setImageBase64(compressed.base64)
  }, [])

  const onSubmit = useCallback(() => {
    const text = input.trim()
    if (!text) return
    if (blocked) return
    // 多轮追问的上下文由 start() 内部临时组装（见 hooks/diagnosisContext.ts）。
    // 这里只传用户原话：把拼好的上下文再传进去会让 turns 存下它，
    // 下一轮又套一层「第N轮：」，长度指数增长直至撞上后端 4000 字上限。
    start(text, imageBase64)
  }, [input, imageBase64, start, blocked])

  const workorderRecordId = state.result?.record_id ?? null
  const showWorkorderHint =
    !!state.result?.workorder?.工单编号 && !workorderRecordId && !state.result.followup_question

  return (
    <div style={{ maxWidth: 920, margin: '0 auto', padding: '32px 24px 60px' }}>
      <header style={{ marginBottom: 22 }}>
        <h1
          style={{
            fontSize: 21,
            fontWeight: 600,
            margin: 0,
            letterSpacing: -0.3,
            display: 'flex',
            alignItems: 'center',
            gap: 9,
          }}
        >
          故障诊断
          <span
            style={{
              fontSize: 11,
              fontWeight: 500,
              color: 'var(--accent)',
              background: 'var(--accent-soft)',
              border: '0.5px solid var(--accent-border)',
              padding: '2px 8px',
              borderRadius: 20,
              letterSpacing: 0.2,
            }}
          >
            多 Agent 协作
          </span>
        </h1>
        <p style={{ fontSize: 13, color: 'var(--color-text-secondary)', margin: '6px 0 0' }}>
          用自然语言描述故障，系统自动完成检索、诊断、审核与工单生成；资料不足时会明确说明而不是编造。
        </p>
      </header>

      {/* 后端未就绪提示。放在输入区之前，用户第一眼就能看到"现在还不能用、为什么"，
          而不是输完一大段话、点了按钮才失败。
          用 accent 蓝而不是警示色：启动是正常过程，不是出错。 */}
      {blocked && (
        <div
          className="fs-banner fs-rise"
          style={{
            padding: '11px 14px',
            borderRadius: 'var(--radius-md)',
            background: backendDown ? 'var(--danger-soft)' : 'var(--accent-soft)',
            border: `0.5px solid ${backendDown ? 'var(--danger)' : 'var(--accent-border)'}`,
            color: backendDown ? '#791F1F' : '#2E2A5E',
            fontSize: 13,
            lineHeight: 1.6,
            marginBottom: 16,
          }}
        >
          {backendDown ? (
            <>
              <b>后端服务不可达。</b>
              请确认已运行「启动后端.bat」或「启动全部.bat」，然后点顶栏状态灯重新探活。
            </>
          ) : (
            <>
              <b>后端正在启动，请稍候。</b>
              初始化要加载向量库与检索索引，约十几秒。{startupHint(health.ready)}
              <div style={{ marginTop: 3, opacity: 0.85 }}>
                加载完成后本提示会自动消失，无需刷新页面。
              </div>
            </>
          )}
        </div>
      )}

      {/* 服务降级提示。与上面的"不可用"不同：这里**不阻断**诊断——
          知识库为空时 `/diagnose` 仍会诚实地降级并产出带风险标记的工单，
          不让用户试反而是过度反应。
          要解决的是**时机**问题：此前只有首次检索才会发现库是空的，
          那时用户已经白等了一轮 9 次 LLM 调用、拿到一张降级工单，
          却看不出根因是"没跑 build_knowledge_base.py"。
          用 warning 琥珀色（而不是 accent 蓝）：这不是正常过程，是需要注意的状态。 */}
      {health.state === 'degraded' && (
        <div
          className="fs-banner fs-rise"
          data-testid="degraded-banner"
          style={{
            padding: '11px 14px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--warning-soft)',
            border: '0.5px solid var(--warning)',
            color: '#633806',
            fontSize: 13,
            lineHeight: 1.6,
            marginBottom: 16,
          }}
        >
          <b>服务可用，但会降级。</b>
          <ul style={{ margin: '4px 0 0', paddingLeft: 18 }}>
            {readyWarnings(health.ready).map((w) => (
              <li key={w}>{w}</li>
            ))}
          </ul>
          <div style={{ marginTop: 3, opacity: 0.85 }}>
            仍可发起诊断，但结果可能因缺少依据而降级；修好后点顶栏状态灯重新探活。
          </div>
        </div>
      )}

      {/* 输入区 */}
      <div
        className="fs-card"
        style={{
          padding: 16,
          marginBottom: 18,
          transition: 'border-color var(--transition), box-shadow var(--transition)',
        }}
      >
        <label htmlFor="fault-input" style={SR_ONLY}>
          故障描述
        </label>
        <textarea
          id="fault-input"
          aria-describedby="fault-input-counter"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          placeholder="描述故障现象，例如：那台数控机床主轴转起来一顿一顿的，还有怪声，温度也高得离谱"
          rows={4}
          disabled={state.running}
          // 上限与后端 DiagnosisRequest.fault_description 的 max_length 一致。
          // 不设的话，用户在输入框里写超长文本，点下去只会拿到一个 422。
          maxLength={MAX_PAYLOAD_CHARS}
          style={{
            width: '100%',
            boxSizing: 'border-box',
            border: 'none',
            outline: 'none',
            resize: 'vertical',
            fontSize: 14,
            lineHeight: 1.65,
            fontFamily: 'var(--font-sans)',
            background: 'transparent',
            color: 'var(--color-text-primary)',
          }}
        />

        {/* 实时字数：只在接近上限时才提示。一直显示数字会变成噪音，
            而用户真正需要知道"快满了"的时刻，正是他快写超的时候。 */}
        {input.length > MAX_PAYLOAD_CHARS * 0.8 && (
          <div
            id="fault-input-counter"
            style={{
              fontSize: 11.5,
              textAlign: 'right',
              color:
                input.length >= MAX_PAYLOAD_CHARS
                  ? 'var(--danger)'
                  : 'var(--color-text-tertiary)',
            }}
          >
            {input.length} / {MAX_PAYLOAD_CHARS}
            {input.length >= MAX_PAYLOAD_CHARS && '（已达上限，超出部分不会被发送）'}
          </div>
        )}

        {/* 示例快捷入口：只在还没开始输入时出现，避免干扰正式使用 */}
        {!input && !state.running && (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 4 }}>
            <span style={{ fontSize: 11.5, color: 'var(--color-text-tertiary)', alignSelf: 'center' }}>
              试一试：
            </span>
            {SAMPLES.map((s) => (
              <button
                key={s.label}
                onClick={() => setInput(s.text)}
                style={{
                  padding: '3px 10px',
                  fontSize: 12,
                  borderRadius: 20,
                  border: '0.5px solid var(--color-border-secondary)',
                  background: 'var(--color-background-primary)',
                  color: 'var(--color-text-secondary)',
                  cursor: 'pointer',
                  fontFamily: 'inherit',
                  transition: 'all var(--transition)',
                }}
                onMouseEnter={(e) => {
                  e.currentTarget.style.borderColor = 'var(--accent-border)'
                  e.currentTarget.style.color = 'var(--accent)'
                  e.currentTarget.style.background = 'var(--accent-soft)'
                }}
                onMouseLeave={(e) => {
                  e.currentTarget.style.borderColor = 'var(--color-border-secondary)'
                  e.currentTarget.style.color = 'var(--color-text-secondary)'
                  e.currentTarget.style.background = 'var(--color-background-primary)'
                }}
              >
                {s.label}
              </button>
            ))}
          </div>
        )}

        {imagePreview && (
          <div style={{ marginTop: 10, position: 'relative', display: 'inline-block' }}>
            <img
              src={imagePreview}
              alt="设备照片预览"
              style={{
                maxHeight: 110,
                borderRadius: 'var(--radius-sm)',
                border: '0.5px solid var(--color-border-secondary)',
                boxShadow: 'var(--shadow-sm)',
              }}
            />
            <button
              onClick={() => {
                setImagePreview(null)
                setImageBase64('')
                if (fileRef.current) fileRef.current.value = ''
              }}
              style={{
                position: 'absolute',
                top: -6,
                right: -6,
                width: 20,
                height: 20,
                borderRadius: '50%',
                border: 'none',
                background: 'var(--danger)',
                color: '#fff',
                fontSize: 12,
                cursor: 'pointer',
                lineHeight: 1,
                boxShadow: 'var(--shadow-sm)',
              }}
            >
              ×
            </button>
          </div>
        )}

        <div
          style={{
            display: 'flex',
            gap: 8,
            alignItems: 'center',
            marginTop: 12,
            paddingTop: 12,
            borderTop: '0.5px solid var(--color-border-tertiary)',
          }}
        >
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            style={{ display: 'none' }}
            onChange={(e) => void onPickImage(e.target.files?.[0])}
          />
          <button
            onClick={() => fileRef.current?.click()}
            disabled={state.running}
            style={btnStyle(false, state.running)}
          >
            ＋ 上传照片
          </button>

          {state.running ? (
            <button onClick={abort} style={btnStyle(true, false, 'var(--danger)')}>
              中断诊断
            </button>
          ) : (
            <button
              onClick={onSubmit}
              disabled={!input.trim() || blocked}
              style={btnStyle(true, !input.trim() || blocked)}
              title={blocked ? '后端尚未就绪' : undefined}
            >
              {notReady ? '后端启动中…' : backendDown ? '后端不可达' : '开始诊断'}
            </button>
          )}

          {!state.running && (state.result || state.error) && (
            <button onClick={reset} style={btnStyle(false, false)}>
              清空重来
            </button>
          )}

          <span style={{ marginLeft: 'auto', fontSize: 12, color: 'var(--color-text-tertiary)' }}>
            {state.turns.length > 0 && `已进行 ${state.turns.length} 轮`}
          </span>
        </div>
      </div>

      {/* 状态机可视化：诊断中或已完成时显示 */}
      {(state.running || state.result) && (
        <StateMachineView
          visited={state.visited}
          active={state.active}
          debateRound={state.debateRound}
          running={state.running}
        />
      )}

      {/* 进度日志：保留原始 label，供想看细节的用户展开 */}
      {state.progressLog.length > 0 && (
        <details
          style={{
            marginBottom: 18,
            fontSize: 12,
            color: 'var(--color-text-secondary)',
            background: 'var(--color-background-primary)',
            border: '0.5px solid var(--color-border-tertiary)',
            borderRadius: 'var(--radius-md)',
            padding: '9px 12px',
          }}
        >
          <summary style={{ cursor: 'pointer', fontWeight: 500 }}>
            执行日志（{state.progressLog.length} 步）
          </summary>
          <div
            style={{
              fontFamily: 'var(--font-mono)',
              lineHeight: 1.7,
              padding: '8px 10px',
              background: 'var(--color-background-tertiary)',
              borderRadius: 'var(--radius-sm)',
              marginTop: 8,
              maxHeight: 260,
              overflow: 'auto',
            }}
          >
            {state.progressLog.map((line, i) => (
              <div key={i}>{line}</div>
            ))}
          </div>
        </details>
      )}

      {/* 错误 */}
      {state.error && (
        <div
          className="fs-banner fs-rise"
          style={{
            padding: '11px 14px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--danger-soft)',
            border: '0.5px solid var(--danger)',
            color: '#791f1f',
            fontSize: 13,
            lineHeight: 1.6,
            marginBottom: 16,
          }}
        >
          {state.error}
          {state.retryAfter && (
            <div style={{ marginTop: 4, fontSize: 12 }}>
              系统当前并发已满（这是保护机制，不是你的请求有问题），建议 {state.retryAfter} 秒后重试。
            </div>
          )}
        </div>
      )}

      {/* 图片未识别提示。传了照片却只按文字诊断，用户必须能看出来——
          否则他会以为照片被用上了，把结论当成"看过照片"得出的。 */}
      {state.result?.image_warning && (
        <div
          className="fs-banner fs-rise"
          data-testid="image-warning"
          style={{
            padding: '11px 14px',
            borderRadius: 'var(--radius-md)',
            background: 'var(--warning-soft)',
            border: '0.5px solid var(--warning)',
            color: '#633806',
            fontSize: 13,
            lineHeight: 1.6,
            marginBottom: 16,
          }}
        >
          {state.result.image_warning}
        </div>
      )}

      {/* 结果 */}
      {state.result && (
        <div className="fs-rise">
          <ResultView result={state.result} />
          {workorderRecordId ? (
            <a
              href={workorderUrl(workorderRecordId)}
              download
              style={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
                marginTop: 6,
                padding: '8px 16px',
                borderRadius: 'var(--radius-md)',
                background: 'linear-gradient(135deg, #6a60d0 0%, #4b3fa8 100%)',
                color: '#fff',
                fontSize: 13,
                fontWeight: 500,
                textDecoration: 'none',
                boxShadow: '0 2px 8px rgba(83, 74, 183, 0.24)',
              }}
            >
              ↓ 下载工单（Markdown）
            </a>
          ) : (
            showWorkorderHint && (
              <p style={{ fontSize: 12, color: 'var(--color-text-tertiary)', marginTop: 6 }}>
                工单已生成但未落库（本次未返回记录 id），可稍后在历史页查看并下载。
              </p>
            )
          )}
        </div>
      )}
    </div>
  )
}
/** 把图片压到最长边 1280px、JPEG 质量 0.85，避免 base64 超限。 */
async function compressImage(file: File): Promise<{ dataUrl: string; base64: string }> {
  const dataUrl = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => resolve(reader.result as string)
    reader.onerror = () => reject(new Error('读取图片失败'))
    reader.readAsDataURL(file)
  })

  const img = await new Promise<HTMLImageElement>((resolve, reject) => {
    const el = new Image()
    el.onload = () => resolve(el)
    el.onerror = () => reject(new Error('图片解码失败'))
    el.src = dataUrl
  })

  const maxEdge = 1280
  const scale = Math.min(1, maxEdge / Math.max(img.width, img.height))
  if (scale === 1 && file.size < 1024 * 1024) {
    return { dataUrl, base64: dataUrl.split(',')[1] ?? '' }
  }

  const canvas = document.createElement('canvas')
  canvas.width = Math.round(img.width * scale)
  canvas.height = Math.round(img.height * scale)
  const ctx = canvas.getContext('2d')
  if (!ctx) return { dataUrl, base64: dataUrl.split(',')[1] ?? '' }
  ctx.drawImage(img, 0, 0, canvas.width, canvas.height)

  const out = canvas.toDataURL('image/jpeg', 0.85)
  return { dataUrl: out, base64: out.split(',')[1] ?? '' }
}
