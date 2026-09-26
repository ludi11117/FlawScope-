/**
 * 状态机前端镜像的测试。
 *
 * 重点守两件事：
 *   1. **节点映射不能漂移**——后端的 label 文案改了，前端的高亮不能跟着错位。
 *      这里用"序号"作为匹配键，测试要断言序号唯一，否则会串台。
 *   2. **进度不倒退**——辩论环会重复访问节点，状态渲染若把已完成的节点
 *      改回"进行中"，用户会看到进度条左右横跳。computeNodeStates 只表达
 *      "走过没走过"，轮次由 debate_round 另行承载。
 */

import { describe, expect, it } from 'vitest'
import {
  NODES,
  PRIMARY_NODES,
  nodeById,
  nodeFromLabel,
  computeNodeStates,
  type NodeId,
} from './machine'

describe('NODES 定义', () => {
  it('共 10 个节点 = 9 个主节点 + 转人工', () => {
    // 这条断言被**加强**过（原为 `NODES.length === 9`）：
    // 转人工必须并进 NODES，否则它触发时状态机视图上没有任何 active 节点，
    // 用户会以为卡死了（见 NodeMeta.conditional）。这里把两个数字都钉住：
    // 总数对了但主节点数错了，同样会红。
    expect(NODES.length).toBe(10)
    expect(PRIMARY_NODES.length).toBe(9)
  })

  it('转人工是唯一的条件节点，且不在进度分母里', () => {
    const conditional = NODES.filter((n) => n.conditional).map((n) => n.id)
    expect(conditional).toEqual(['human_review'])
    expect(PRIMARY_NODES.map((n) => n.id)).not.toContain('human_review')
  })

  it('序号唯一 —— 否则 nodeFromLabel 会匹配错节点', () => {
    const indexes = NODES.map((n) => n.index)
    expect(new Set(indexes).size).toBe(indexes.length)
  })

  it('id 唯一', () => {
    const ids = NODES.map((n) => n.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it('辩论环包含且仅包含 review / rebuttal / final_review', () => {
    const loop = NODES.filter((n) => n.inDebateLoop).map((n) => n.id)
    expect(new Set(loop)).toEqual(new Set(['review', 'rebuttal', 'final_review']))
  })
})

describe('nodeFromLabel', () => {
  it('按序号匹配真实的后端 label', () => {
    expect(nodeFromLabel('① 信息抽取：把口语化描述转成结构化字段')?.id).toBe('extract_info')
    expect(nodeFromLabel('③ 知识检索：混合检索相关故障资料')?.id).toBe('retrieve')
    expect(nodeFromLabel('⑨ 工单生成：汇总输出维修工单')?.id).toBe('workorder')
  })

  it('文案改变但序号不变时仍能匹配（这正是用序号而非标题的原因）', () => {
    expect(nodeFromLabel('⑤ 完全换了个说法')?.id).toBe('review')
  })

  it('中断事件不映射到任何节点（它不是状态机的一步）', () => {
    expect(nodeFromLabel('⚠️ 诊断流程异常中断，已生成待人工处理工单')).toBeNull()
  })

  it('启动事件不映射到节点', () => {
    expect(nodeFromLabel('🚀 启动多Agent协作诊断...')).toBeNull()
  })

  it('转人工映射到 human_review', () => {
    // 这里刻意保留 `nodeFromLabel(...).id` 的写法：如果哪天 nodeFromLabel 返回 null，
    // 测试应当以"读不到 id"的形式失败，而不是被可选链静默吞掉变成永远通过。
    // 为满足 noUncheckedIndexedAccess/strict 下的类型检查，先断言非空再取值。
    const node = nodeFromLabel('转人工复核')
    expect(node).not.toBeNull()
    expect(node!.id).toBe('human_review')
  })

  it('反向: 无序号无缘由的文本不应被误映射', () => {
    expect(nodeFromLabel('随便一句没有任何标记的话')).toBeNull()
  })
})

describe('nodeById', () => {
  it('已知 id 返回对应节点', () => {
    expect(nodeById('diagnose').title).toBe('初步诊断')
  })

  it('未知 id 返回兜底项而不是 undefined（避免调用处崩）', () => {
    expect(nodeById('nonexistent' as NodeId)).toBeDefined()
  })
})

describe('computeNodeStates', () => {
  it('未访问且非活跃 → pending', () => {
    const states = computeNodeStates(new Set(), null)
    expect(states.get('extract_info')).toBe('pending')
  })

  it('已访问 → done', () => {
    const states = computeNodeStates(new Set<NodeId>(['extract_info']), null)
    expect(states.get('extract_info')).toBe('done')
  })

  it('活跃优先于已访问（正在跑的节点显示进行中）', () => {
    const states = computeNodeStates(new Set<NodeId>(['review']), 'review')
    expect(states.get('review')).toBe('active')
  })

  it('辩论环重复访问不让已完成节点回退', () => {
    // diagnose 走过一次，之后辩论环在 review/rebuttal 之间循环，
    // diagnose 必须一直是 done —— 否则进度条会看起来在倒退
    const visited = new Set<NodeId>(['extract_info', 'check_info', 'retrieve', 'diagnose', 'review'])
    const states = computeNodeStates(visited, 'rebuttal')
    expect(states.get('diagnose')).toBe('done')
    expect(states.get('review')).toBe('done')
    expect(states.get('rebuttal')).toBe('active')
    expect(states.get('final_review')).toBe('pending')
  })

  it('返回所有节点的状态，不漏项', () => {
    const states = computeNodeStates(new Set(), null)
    for (const n of NODES) {
      expect(states.has(n.id)).toBe(true)
    }
  })

  it('转人工时必须有一个 active 节点（否则界面看起来像卡死）', () => {
    // 修复前 NODES 里没有 human_review，转人工时 computeNodeStates 返回的 map
    // 里根本没有这个键，状态机视图上**没有任何节点是进行中** ——
    // 用户看到的是"卡住了"，而系统其实在等他做人工复核。
    const states = computeNodeStates(new Set<NodeId>(['review']), 'human_review')

    expect(states.get('human_review')).toBe('active')
  })
})
