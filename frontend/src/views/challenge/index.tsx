import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/challenge'
const columns = [
  "挑战编号",
  "渲染配置编号",
  "验证码类型",
  "识别服务商",
  "识别超时秒",
  "重试次数上限",
  "已尝试次数",
  "识别状态",
  "分流结论",
  "打码等待队列",
  "页面等待条件",
  "点击选择器",
  "降级策略",
  "降级原因",
  "status"
]
const statuses = [
  "待分流",
  "自动识别中",
  "待人工打码",
  "人工认领中",
  "已完成",
  "已降级",
  "已归档",
  "已恢复"
]
const actionLabels = [
  "认领打码",
  "提交人工结果",
  "归档挑战",
  "恢复挑战"
]

type Row = Record<string, string | number | boolean | null>

type QueueItem = {
  挑战编号: string
  渲染配置编号: string
  验证码类型: string
  打码等待队列: string
  页面等待条件: string
  点击选择器: string
  剩余秒: number | null
  已超时: boolean
}

type Summary = {
  总数: number
  状态分布: Record<string, number>
  待人工打码: number
  已降级: number
  服务商名额: { 识别服务商: string; 已占用: number; 名额上限: number; 可用: number }[]
}

export default function ChallengeView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [summary, setSummary] = useState<Summary | null>(null)
  const [queue, setQueue] = useState<QueueItem[]>([])
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => ["挑战编号", "status"], [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const [listResponse, summaryResponse, queueResponse] = await Promise.all([
        request(`${ENDPOINT}?${query}`),
        request(`${ENDPOINT}/summary`),
        request(`${ENDPOINT}/queue`),
      ])
      if (!listResponse.ok || !summaryResponse.ok || !queueResponse.ok) {
        throw new Error('挑战分流数据读取失败')
      }
      const listPayload = await listResponse.json()
      const summaryPayload = await summaryResponse.json()
      const queuePayload = await queueResponse.json()
      setRows(listPayload.items ?? [])
      setTotal(listPayload.total ?? 0)
      setSummary(summaryPayload)
      setQueue(queuePayload.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '挑战分流列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function post(path: string, body: Record<string, unknown>) {
    setErrorMessage('')
    setNotice('')
    try {
      const response = await request(path, { method: 'POST', body: JSON.stringify({ values: body }) })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error('挑战分流动作未送达')
      }
      if (!payload.ok) {
        setErrorMessage(payload.message)
      } else {
        setNotice(payload.message)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '挑战分流操作失败')
    }
  }

  async function runAction(action: string, row: Row) {
    const id = row.id
    if (action === "认领打码") {
      await post(`${ENDPOINT}/${id}/claim`, { 操作人: "值班打码员" })
      return
    }
    if (action === "提交人工结果") {
      await post(`${ENDPOINT}/${id}/manual-result`, { 识别状态: "成功", 操作人: "值班打码员" })
      return
    }
    if (action === "归档挑战") {
      await post(`${ENDPOINT}/${id}/actions`, { action, 操作人: "值班管理员" })
      return
    }
    await post(`${ENDPOINT}/${id}/actions`, { action, 操作人: "值班管理员" })
  }

  return (
    <section className="page" data-module="challenge">
      <header className="page-head">
        <div>
          <h2>验证码挑战分流</h2>
          <p className="page-desc">
            渲染测试命中需识别页面时，按验证码类型、识别服务商、超时与重试上限决定自动识别、人工打码或降级渲染；
            降级结论会回写到渲染结果，队列等待不阻塞其他站点采集。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => void post(`${ENDPOINT}/expire-queue`, {})}>
            处理超时队列
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出挑战分流清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">挑战总数</span>
          <strong className="stat-value">{summary?.总数 ?? 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">待人工打码</span>
          <strong className="stat-value">{summary?.待人工打码 ?? 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">已降级</span>
          <strong className="stat-value">{summary?.已降级 ?? 0}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">服务商名额占用</span>
          <strong className="stat-value">
            {(summary?.服务商名额 ?? []).map((item) => `${item.识别服务商} ${item.已占用}/${item.名额上限}`).join('，') || '—'}
          </strong>
        </article>
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field === "status" ? "状态" : field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field === "status" ? "状态" : field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
        <span className="filter-item"><span>状态可选</span>{statuses.join('、')}</span>
      </form>

      <h3>打码等待队列</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>挑战编号</th>
            <th>渲染配置</th>
            <th>验证码类型</th>
            <th>打码等待队列</th>
            <th>页面等待条件</th>
            <th>点击选择器</th>
            <th>剩余秒</th>
          </tr>
        </thead>
        <tbody>
          {queue.map((item) => (
            <tr key={item.挑战编号}>
              <td>{item.挑战编号}</td>
              <td>{item.渲染配置编号}</td>
              <td>{item.验证码类型}</td>
              <td>{item.打码等待队列}</td>
              <td>{item.页面等待条件}</td>
              <td>{item.点击选择器}</td>
              <td>{item.已超时 ? '已超时' : item.剩余秒 ?? '—'}</td>
            </tr>
          ))}
          {queue.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={7}>当前没有等待人工打码的挑战</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3>挑战分流记录</h3>
      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column === "status" ? "状态" : column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] === '' ? '—' : String(row[column] ?? '—')}</td>)}
              <td className="row-actions">
                {actionLabels.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无挑战分流记录，可在JS渲染采集页发起渲染测试</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条挑战记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
