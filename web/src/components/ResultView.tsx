/**
 * 诊断结果展示。
 *
 * 两条硬约束在这里落地：
 *   1. **降级工单不得补空章节**（项目硬约束 20）：没有依据的字段就不渲染，
 *      不能因为"布局好看"给降级工单补出空的"维修方案"——那是编造。
 *   2. **RiskLevel 必须显著**：`required_review` / `degraded` 类结果不能
 *      和正常结果长得一样，否则操作工可能照着一张"待人工确认"的工单去拆机。
 */

import type { DiagnosisResult, DiagnosisStatus } from '../types/contracts'
import { isTerminalFailure } from '../types/contracts'

interface Props {
  result: DiagnosisResult
}

/** 状态 → 中文说明 + 配色。与 app.py 的状态横幅口径保持一致。 */
const STATUS_META: Record<
  string,
  { label: string; bg: string; border: string; text: string; icon: string }
> = {
  done: { label: '诊断完成', bg: '#E1F5EE', border: '#0F6E56', text: '#04342C', icon: '✓' },
  need_more_info: {
    label: '需要补充信息',
    bg: '#FAEEDA',
    border: '#854F0B',
    text: '#412402',
    icon: '?',
  },
  insufficient_knowledge: {
    label: '知识库无相关依据，无法自动诊断',
    bg: '#FAEEDA',
    border: '#854F0B',
    text: '#412402',
    icon: '!',
  },
  llm_failed: {
    label: '模型服务调用失败',
    bg: '#FCEBEB',
    border: '#A32D2D',
    text: '#791F1F',
    icon: '×',
  },
  pending_human_review: {
    label: '转人工复核',
    bg: '#FCEBEB',
    border: '#A32D2D',
    text: '#791F1F',
    icon: '⚠',
  },
}

function statusMeta(status: DiagnosisStatus) {
  return (
    STATUS_META[status] ?? {
      label: status,
      bg: '#F1EFE8',
      border: '#888780',
      text: '#2C2C2A',
      icon: '•',
    }
  )
}

/** 分区卡片：每块独立成卡，视觉上把"诊断/辩论/成本/工单"分开 */
function Section({
  title,
  badge,
  children,
}: {
  title: string
  badge?: React.ReactNode
  children: React.ReactNode
}) {
  return (
    <section
      style={{
        background: 'var(--color-background-primary)',
        border: '1px solid var(--color-border-tertiary)',
        borderRadius: 'var(--radius-lg)',
        padding: '14px 16px 15px',
        marginBottom: 14,
        boxShadow: 'var(--shadow-sm)',
      }}
    >
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 10,
          paddingBottom: 8,
          borderBottom: '0.5px solid var(--color-border-tertiary)',
        }}
      >
        <span
          aria-hidden
          style={{
            width: 3,
            height: 13,
            borderRadius: 2,
            background: 'var(--accent)',
            flexShrink: 0,
          }}
        />
        <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--color-text-primary)' }}>
          {title}
        </span>
        {badge}
      </div>
      {children}
    </section>
  )
}

function Field({ label, value }: { label: string; value?: React.ReactNode }) {
  if (value === undefined || value === null || value === '') return null
  return (
    <div style={{ display: 'flex', gap: 10, marginBottom: 7, fontSize: 13, lineHeight: 1.65 }}>
      <span
        style={{
          color: 'var(--color-text-secondary)',
          minWidth: 68,
          flexShrink: 0,
          fontSize: 12.5,
          paddingTop: 1,
        }}
      >
        {label}
      </span>
      <span style={{ color: 'var(--color-text-primary)', flex: 1 }}>{value}</span>
    </div>
  )
}

/** 小徽章 */
function Tag({ children, tone = 'neutral' }: { children: React.ReactNode; tone?: string }) {
  const NEUTRAL = {
    bg: 'var(--color-background-tertiary)',
    fg: 'var(--color-text-secondary)',
    bd: 'transparent',
  }
  const tones: Record<string, { bg: string; fg: string; bd: string }> = {
    neutral: NEUTRAL,
    accent: { bg: 'var(--accent-soft)', fg: 'var(--accent)', bd: 'var(--accent-border)' },
    warn: { bg: 'var(--warning-soft)', fg: 'var(--warning)', bd: 'transparent' },
  }
  const t = tones[tone] ?? NEUTRAL
  return (
    <span
      style={{
        fontSize: 11,
        padding: '1px 7px',
        borderRadius: 5,
        background: t.bg,
        color: t.fg,
        border: `0.5px solid ${t.bd}`,
        fontWeight: 500,
      }}
    >
      {children}
    </span>
  )
}

export function ResultView({ result }: Props) {
  const meta = statusMeta(result.status)
  const failed = isTerminalFailure(result.status)
  const wo = result.workorder ?? {}
  const dg = result.diagnosis ?? {}
  const cost = result.cost ?? {}
  const hasDiagnosis = Object.keys(dg).length > 0
  const hasWorkorder = Object.keys(wo).length > 0

  return (
    <div>
      {/* 状态横幅：降级 / 失败必须一眼可辨 */}
      <div
        className="fs-banner"
        style={{
          padding: '12px 15px',
          borderRadius: 'var(--radius-md)',
          background: meta.bg,
          border: `0.5px solid ${meta.border}`,
          color: meta.text,
          fontSize: 13.5,
          fontWeight: 600,
          marginBottom: 16,
          display: 'flex',
          alignItems: 'center',
          gap: 9,
        }}
      >
        <span
          aria-hidden
          style={{
            width: 20,
            height: 20,
            borderRadius: '50%',
            display: 'grid',
            placeItems: 'center',
            fontSize: 12,
            background: meta.border,
            color: '#fff',
            flexShrink: 0,
          }}
        >
          {meta.icon}
        </span>
        {meta.label}
        {result.correlation_id && (
          <span
            style={{
              marginLeft: 'auto',
              fontWeight: 400,
              fontSize: 11,
              opacity: 0.7,
              fontFamily: 'var(--font-mono)',
            }}
            title="全链路追踪 ID"
          >
            {result.correlation_id}
          </span>
        )}
      </div>

      {/* 追问：这是"信息不足"分支的唯一出路，必须比诊断结果更显眼 */}
      {result.followup_question && (
        <div
          style={{
            padding: '13px 15px',
            borderRadius: 'var(--radius-md)',
            background: 'linear-gradient(135deg, #fdf7e8 0%, #fbf1dc 100%)',
            border: '0.5px solid #ef9f27',
            fontSize: 13.5,
            lineHeight: 1.65,
            marginBottom: 16,
            display: 'flex',
            gap: 10,
          }}
        >
          <span
            aria-hidden
            style={{
              width: 22,
              height: 22,
              borderRadius: '50%',
              background: '#ef9f27',
              color: '#fff',
              display: 'grid',
              placeItems: 'center',
              fontSize: 12,
              flexShrink: 0,
              fontWeight: 600,
            }}
          >
            ?
          </span>
          <div>
            <b>需要你补充：</b>
            {result.followup_question}
          </div>
        </div>
      )}

      {/* 诊断结论。降级时不渲染"根因判断"框——那是编造 */}
      {hasDiagnosis && (
        <Section title="诊断结论" badge={dg.报警代码 ? <Tag tone="accent">{dg.报警代码}</Tag> : undefined}>
          {dg.根因判断 && (
            <div
              style={{
                background: 'var(--color-background-secondary)',
                borderLeft: '3px solid var(--accent)',
                borderRadius: '0 var(--radius-sm) var(--radius-sm) 0',
                padding: '10px 13px',
                marginBottom: 11,
                fontSize: 13.5,
                lineHeight: 1.7,
                color: 'var(--color-text-primary)',
              }}
            >
              {dg.根因判断}
            </div>
          )}
          <Field label="依据" value={dg.依据} />
          {dg.排查建议 && dg.排查建议.length > 0 && (
            <div style={{ marginTop: 4 }}>
              <div
                style={{
                  fontSize: 12.5,
                  color: 'var(--color-text-secondary)',
                  marginBottom: 6,
                  marginTop: 9,
                }}
              >
                排查建议
              </div>
              <ol style={{ margin: 0, paddingLeft: 0, listStyle: 'none' }}>
                {dg.排查建议.map((s, i) => (
                  <li
                    key={i}
                    style={{
                      display: 'flex',
                      gap: 9,
                      marginBottom: 6,
                      fontSize: 13,
                      lineHeight: 1.6,
                    }}
                  >
                    <span
                      aria-hidden
                      style={{
                        width: 18,
                        height: 18,
                        borderRadius: '50%',
                        background: 'var(--accent-soft)',
                        color: 'var(--accent)',
                        fontSize: 11,
                        fontWeight: 600,
                        display: 'grid',
                        placeItems: 'center',
                        flexShrink: 0,
                        marginTop: 1,
                      }}
                    >
                      {i + 1}
                    </span>
                    <span>{s}</span>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </Section>
      )}

      {/* 辩论过程：只在真的辩论过时展示，且明确标出轮数与结果 */}
      {result.debate_round > 0 && (
        <Section
          title="辩论过程"
          badge={
            <Tag tone="warn">{result.debate_round} 轮</Tag>
          }
        >
          {result.review && Object.keys(result.review).length > 0 && (
            <div style={{ marginBottom: 12 }}>
              <div
                style={{
                  fontSize: 11.5,
                  color: 'var(--color-text-tertiary)',
                  marginBottom: 5,
                  letterSpacing: 0.3,
                }}
              >
                审核意见
              </div>
              <Field label="结论" value={(result.review as Record<string, string>).审核意见} />
              <Field label="理由" value={(result.review as Record<string, string>).理由} />
            </div>
          )}
          {result.rebuttal && Object.keys(result.rebuttal).length > 0 && (
            <div
              style={{
                marginBottom: 12,
                paddingLeft: 12,
                borderLeft: '2px solid var(--color-border-secondary)',
              }}
            >
              <div
                style={{
                  fontSize: 11.5,
                  color: 'var(--color-text-tertiary)',
                  marginBottom: 5,
                  letterSpacing: 0.3,
                }}
              >
                诊断师反驳
              </div>
              <Field label="最终根因" value={result.rebuttal.最终根因} />
              <Field label="反驳理由" value={result.rebuttal.反驳理由} />
              <Field label="置信度" value={result.rebuttal.置信度} />
            </div>
          )}
          {result.final_review && Object.keys(result.final_review).length > 0 && (
            <div>
              <div
                style={{
                  fontSize: 11.5,
                  color: 'var(--color-text-tertiary)',
                  marginBottom: 5,
                  letterSpacing: 0.3,
                }}
              >
                最终复审
              </div>
              <Field label="结论" value={result.final_review.审核意见} />
              <Field
                label="置信度"
                value={
                  result.final_review.置信度 !== undefined
                    ? `${result.final_review.置信度}%`
                    : undefined
                }
              />
            </div>
          )}
        </Section>
      )}

      {/* 成本。N/A 是有效信息，不能当成"没有"直接隐藏 */}
      {(cost.预计成本 || cost.备件清单?.length || cost.计费提示) && (
        <Section title="成本核算">
          <div style={{ display: 'flex', gap: 26, marginBottom: 10 }}>
            {cost.预计成本 && (
              <div>
                <div style={{ fontSize: 11.5, color: 'var(--color-text-tertiary)' }}>预计成本</div>
                <div
                  style={{
                    fontSize: 19,
                    fontWeight: 600,
                    color: 'var(--accent)',
                    letterSpacing: -0.3,
                  }}
                >
                  {cost.预计成本}
                </div>
              </div>
            )}
            {cost.预计工时 && (
              <div>
                <div style={{ fontSize: 11.5, color: 'var(--color-text-tertiary)' }}>预计工时</div>
                <div style={{ fontSize: 19, fontWeight: 600, letterSpacing: -0.3 }}>
                  {cost.预计工时}
                </div>
              </div>
            )}
          </div>
          {cost.备件清单 && cost.备件清单.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginBottom: 4 }}>
              {cost.备件清单.map((p, i) => (
                <Tag key={i}>{p}</Tag>
              ))}
            </div>
          )}
          {/* 计费提示是"未覆盖备件"的兜底说明，必须显示，否则用户以为报价是完整的 */}
          {cost.计费提示 && (
            <div
              style={{
                marginTop: 8,
                fontSize: 12,
                color: 'var(--warning)',
                padding: '7px 10px',
                background: 'var(--warning-soft)',
                borderRadius: 'var(--radius-sm)',
                border: '0.5px solid #EF9F27',
              }}
            >
              {cost.计费提示}
            </div>
          )}
        </Section>
      )}

      {/* 工单：只渲染实际存在的字段，缺失的不补占位（硬约束 20） */}
      {hasWorkorder && (
        <Section
          title="维修工单"
          badge={wo.工单编号 ? <Tag>{wo.工单编号}</Tag> : undefined}
        >
          {wo.风险等级 && (
            <div
              style={{
                padding: '10px 12px',
                borderRadius: 'var(--radius-sm)',
                background: failed ? 'var(--danger-soft)' : 'var(--warning-soft)',
                border: `0.5px solid ${failed ? 'var(--danger)' : '#EF9F27'}`,
                color: failed ? '#791F1F' : '#412402',
                fontSize: 13,
                fontWeight: 600,
                marginBottom: 11,
                display: 'flex',
                gap: 8,
              }}
            >
              <span aria-hidden>⚠</span>
              <div>
                {wo.风险等级}
                {wo.风险说明 && (
                  // pre-line：风险说明是**多行**文本（降级时会带"参考方向"的
                  // 逐条排查动作）。不设的话换行会被 HTML 折叠成空格，几条动作挤成一坨。
                  <div
                    style={{
                      fontWeight: 400,
                      marginTop: 4,
                      lineHeight: 1.6,
                      whiteSpace: 'pre-line',
                    }}
                  >
                    {wo.风险说明}
                  </div>
                )}
              </div>
            </div>
          )}
          <Field label="故障现象" value={wo.故障现象} />
          <Field label="根因" value={wo.根因} />
          <Field label="维修方案" value={wo.维修方案} />
          <Field label="备件清单" value={wo.备件清单?.join('、')} />
          <Field label="预计成本" value={wo.预计成本} />
          <Field label="安全注意" value={wo.安全注意事项} />
        </Section>
      )}

      {/* Token 用量：可观测性的对外呈现 */}
      {result.token_usage && Object.keys(result.token_usage).length > 0 && (
        <Section title="本次用量">
          <div style={{ display: 'flex', gap: 26, fontSize: 13 }}>
            {result.token_usage.总Token !== undefined && (
              <div>
                <div style={{ fontSize: 11.5, color: 'var(--color-text-tertiary)' }}>Token</div>
                <div style={{ fontWeight: 600, fontSize: 16 }}>
                  {Number(result.token_usage.总Token).toLocaleString()}
                </div>
              </div>
            )}
            {result.token_usage.总耗时 !== undefined && (
              <div>
                <div style={{ fontSize: 11.5, color: 'var(--color-text-tertiary)' }}>耗时</div>
                <div style={{ fontWeight: 600, fontSize: 16 }}>
                  {(Number(result.token_usage.总耗时) / 1000).toFixed(1)}s
                </div>
              </div>
            )}
            <div>
              <div style={{ fontSize: 11.5, color: 'var(--color-text-tertiary)' }}>辩论轮数</div>
              <div style={{ fontWeight: 600, fontSize: 16 }}>{result.debate_round}</div>
            </div>
          </div>
        </Section>
      )}
    </div>
  )
}
