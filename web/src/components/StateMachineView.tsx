/**
 * 诊断过程可视化：把 9 个节点的执行过程画成一条水平进度链。
 *
 * 这是相比 Streamlit 版最大的增量——原版只能显示一行行滚动的文字日志，
 * 看不出"走到哪了、还剩几步、辩论环走了没有"。诊断一次要几十秒，
 * 用户在这段时间里最需要的正是这个。
 *
 * 辩论环（review → rebuttal → final_review）单独用高亮标识：
 * 它是本项目多 Agent 架构的核心，也是"单 Agent 做不到"的论据所在。
 */

import type { NodeId, NodeState } from '../state/machine'
import { NODES, PRIMARY_NODES, computeNodeStates } from '../state/machine'

interface Props {
  visited: Set<NodeId>
  active: NodeId | null
  debateRound: number
  running: boolean
}

const STATE_STYLE: Record<
  NodeState,
  { bg: string; border: string; text: string; dot: string }
> = {
  pending: { bg: '#FBFAF8', border: 'rgba(0,0,0,0.1)', text: '#9a9893', dot: '#d3d1c7' },
  active: { bg: '#EAF2FC', border: '#185FA5', text: '#042C53', dot: '#185FA5' },
  done: { bg: '#E9F6F1', border: 'rgba(15,110,86,0.28)', text: '#0b5345', dot: '#0F6E56' },
}

export function StateMachineView({ visited, active, debateRound, running }: Props) {
  const states = computeNodeStates(visited, active)
  const debateEntered = visited.has('review') || active === 'review'
  // 进度分母用 PRIMARY_NODES（正常链路的 9 个节点），不含"转人工"。
  // 转人工是条件分支：算进分母会让正常走完的一次诊断停在 9/10，
  // 看起来像"没跑完"，而它其实已经完整结束了。
  const doneCount = PRIMARY_NODES.filter((n) => states.get(n.id) === 'done').length
  const pct = Math.round((doneCount / PRIMARY_NODES.length) * 100)

  return (
    <div
      style={{
        background: 'var(--color-background-primary)',
        border: '1px solid var(--color-border-tertiary)',
        borderRadius: 'var(--radius-lg)',
        padding: '14px 16px 16px',
        marginBottom: 18,
        boxShadow: 'var(--shadow-sm)',
      }}
    >
      {/* 头部：标题 + 进度百分比 + 辩论状态 */}
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'space-between',
          marginBottom: 12,
        }}
      >
        <span
          style={{
            fontSize: 13,
            fontWeight: 600,
            color: 'var(--color-text-primary)',
            display: 'flex',
            alignItems: 'center',
            gap: 8,
          }}
        >
          <span
            aria-hidden
            style={{ width: 3, height: 13, borderRadius: 2, background: 'var(--accent)' }}
          />
          诊断过程
          <span style={{ fontSize: 11.5, fontWeight: 400, color: 'var(--color-text-tertiary)' }}>
            {doneCount}/{PRIMARY_NODES.length}
          </span>
        </span>
        <span style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          {debateEntered && (
            <span
              style={{
                fontSize: 11,
                fontWeight: 500,
                color: 'var(--warning)',
                background: 'var(--warning-soft)',
                padding: '2px 8px',
                borderRadius: 20,
              }}
            >
              辩论环第 {debateRound || 1} 轮
            </span>
          )}
          {running && !debateEntered && (
            <span style={{ fontSize: 11.5, color: 'var(--color-text-tertiary)' }}>
              <span className="fs-dot" />进行中…
            </span>
          )}
        </span>
      </div>

      {/* 进度条：给整条链路一个总体完成度 */}
      <div
        style={{
          height: 3,
          borderRadius: 2,
          background: 'var(--color-background-tertiary)',
          marginBottom: 14,
          overflow: 'hidden',
        }}
      >
        <div
          style={{
            width: `${pct}%`,
            height: '100%',
            borderRadius: 2,
            background: 'linear-gradient(90deg, #6a60d0 0%, #0F6E56 100%)',
            transition: 'width 0.4s ease',
          }}
        />
      </div>

      {/* 节点链 */}
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 0, flexWrap: 'wrap' }}>
        {NODES.map((node, i) => {
          const s = states.get(node.id) ?? 'pending'
          const style = STATE_STYLE[s]
          const isActive = s === 'active'
          return (
            <div key={node.id} style={{ display: 'flex', alignItems: 'center' }}>
              <div
                title={node.detail}
                style={{
                  minWidth: 82,
                  padding: '8px 10px',
                  borderRadius: 'var(--radius-md)',
                  background: style.bg,
                  border: `1px solid ${style.border}`,
                  borderWidth: isActive ? 1.5 : 1,
                  color: style.text,
                  fontSize: 12,
                  lineHeight: 1.35,
                  transition: 'all 0.25s ease',
                  animation: isActive ? 'fs-pulse 1.6s ease-out infinite' : undefined,
                  position: 'relative',
                  boxShadow: s === 'done' ? 'none' : 'var(--shadow-sm)',
                }}
              >
                <div
                  style={{ display: 'flex', alignItems: 'center', gap: 5, whiteSpace: 'nowrap' }}
                >
                  <span
                    aria-hidden
                    style={{
                      width: 6,
                      height: 6,
                      borderRadius: '50%',
                      background: style.dot,
                      flexShrink: 0,
                      boxShadow: isActive ? `0 0 0 2.5px ${style.dot}33` : 'none',
                    }}
                  />
                  <span style={{ fontWeight: s === 'pending' ? 400 : 600 }}>
                    {node.index} {node.title}
                  </span>
                </div>
                {node.inDebateLoop && (
                  <div
                    style={{
                      position: 'absolute',
                      top: 4,
                      right: 5,
                      width: 5,
                      height: 5,
                      borderRadius: '50%',
                      background: debateEntered ? '#854F0B' : '#D3D1C7',
                      transition: 'background 0.25s',
                    }}
                    title="属于辩论环"
                  />
                )}
              </div>
              {i < NODES.length - 1 && (
                <div
                  style={{
                    width: 16,
                    height: 2,
                    borderRadius: 1,
                    background:
                      s === 'done'
                        ? 'linear-gradient(90deg, #0F6E56 0%, rgba(15,110,86,0.35) 100%)'
                        : 'var(--color-background-tertiary)',
                    transition: 'background 0.4s',
                  }}
                />
              )}
            </div>
          )
        })}
      </div>

      {/* 辩论环的分组标注，让"这三步是循环的"在视觉上成立 */}
      {debateEntered && (
        <div
          style={{
            marginTop: 10,
            fontSize: 11,
            color: 'var(--warning)',
            display: 'flex',
            alignItems: 'center',
            gap: 7,
          }}
        >
          <span
            aria-hidden
            style={{
              display: 'inline-block',
              width: 26,
              height: 8,
              border: '0.5px solid #854F0B',
              borderTop: 'none',
              borderRadius: '0 0 4px 4px',
            }}
          />
          审核 ⇄ 反驳 可循环多轮，直到通过或达上限
        </div>
      )}

      <style>{`
        .fs-dot {
          display: inline-block;
          width: 5px;
          height: 5px;
          border-radius: 50%;
          background: #185FA5;
          margin-right: 5px;
          vertical-align: middle;
          animation: fs-blink 1.2s ease-in-out infinite;
        }
        @keyframes fs-blink {
          0%, 100% { opacity: 1; }
          50% { opacity: 0.25; }
        }
      `}</style>
    </div>
  )
}
