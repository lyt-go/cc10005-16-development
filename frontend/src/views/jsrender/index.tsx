import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/jsrender'
const columns = [
  "配置编号",
  "目标URL",
  "浏览器类型",
  "渲染等待秒",
  "滚动次数",
  "点击选择器",
  "页面等待条件",
  "截屏开关",
  "渲染结果"
]
const actions = [
  "提交渲染测试",
  "标记上线",
  "标记降级"
]
const statuses = [
  "草稿",
  "渲染测试中",
  "已上线",
  "已降级"
]
const stats = [
  {
    "label": "渲染配置总数",
    "value": 0
  },
  {
    "label": "已上线配置",
    "value": 0
  },
  {
    "label": "测试中配置",
    "value": 0
  }
]

type Row = Record<string, string | number | null>

function renderResultText(value: string | number | null | undefined) {
  if (!value || typeof value !== 'object') {
    return '—'
  }
  const result = value as Record<string, unknown>
  const parts = [
    result["渲染状态"] ? `渲染：${result["渲染状态"]}` : '',
    result["分流结论"] ? `分流：${result["分流结论"]}` : '',
    result["识别状态"] ? `识别：${result["识别状态"]}` : '',
    result["降级生效"] ? `降级：${result["降级原因"] || result["降级策略"]}` : '',
    result["打码等待队列"] ? `队列：${result["打码等待队列"]}` : '',
  ].filter(Boolean)
  return parts.length ? parts.join('；') : '—'
}

export default function JsrenderView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [notice, setNotice] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('渲染配置列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'JS渲染采集列表读取失败')
    }
  }, [filters])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('JS渲染采集动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : 'JS渲染采集操作失败')
    }
  }

  async function runChallenge(row: Row) {
    setErrorMessage('')
    const policy = window.prompt('请输入本次渲染命中的验证码策略编号（如 CAPT-0002 / CAPT-0003）', 'CAPT-0003')
    if (!policy) {
      return
    }
    try {
      const response = await request('/api/challenge/submit', {
        method: 'POST',
        body: JSON.stringify({ values: { 渲染配置ID: row.id, 验证码策略编号: policy } }),
      })
      const payload = await response.json()
      if (!response.ok) {
        throw new Error('挑战分流请求未送达')
      }
      setErrorMessage(payload.ok ? '' : payload.message)
      if (payload.ok) {
        setNotice(payload.message)
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '挑战分流失败')
    }
  }

  return (
    <section className="page" data-module="jsrender">
      <header className="page-head">
        <div>
          <h2>JS渲染采集管理</h2>
          <p className="page-desc">管理需要JavaScript渲染的动态页面采集配置，定义浏览器环境、渲染等待和交互操作。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('渲染配置登记入口尚未接入审批流')}>
            登记渲染配置
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出JS渲染采集清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => (
                <td key={column}>
                  {column === "渲染结果"
                    ? renderResultText(row[column])
                    : row[column] ?? '—'}
                </td>
              ))}
              <td className="row-actions">
                <button className="link" type="button" onClick={() => void runChallenge(row)}>
                  发起挑战分流
                </button>
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无JS渲染采集数据，可先登记渲染配置</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条JS渲染采集记录</span>
        {notice && <span>{notice}</span>}
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
