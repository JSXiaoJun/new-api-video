const csrf = document.querySelector('meta[name="csrf-token"]').content
const dialog = document.querySelector('#image-upstream-dialog')
const form = document.querySelector('#image-upstream-form')
const toast = document.querySelector('#toast')
const routeRows = document.querySelector('#image-route-rows')
let dashboard = { upstreams: [], requests: [], stats: {} }

function updateResponsiveClass() {
  document.documentElement.classList.toggle('is-mobile', window.matchMedia('(max-width: 760px)').matches)
}

updateResponsiveClass()
window.addEventListener('resize', updateResponsiveClass)

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char])
}

function showToast(message, tone = 'default') {
  toast.textContent = message
  toast.dataset.tone = tone
  toast.hidden = false
  clearTimeout(showToast.timer)
  showToast.timer = setTimeout(() => { toast.hidden = true }, 2800)
}

async function api(url, options = {}) {
  const headers = { ...(options.headers || {}) }
  if (options.method && options.method !== 'GET') headers['X-CSRF-Token'] = csrf
  const response = await fetch(url, { ...options, headers })
  if (response.status === 401) {
    window.location.assign('/admin/login')
    throw new Error('登录已过期')
  }
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}))
    const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join('；') : payload.detail
    throw new Error(detail || `请求失败 (${response.status})`)
  }
  return response.json()
}

function formatTime(timestamp) {
  if (!timestamp) return '—'
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(timestamp * 1000))
}

function formatCost(value) {
  return `¥${Number(value || 0).toFixed(4).replace(/0+$/, '').replace(/\.$/, '')}`
}

function healthLabel(state) {
  return { stable: '稳定', unobserved: '待观测', pressure: '即时异常' }[state] || state
}

function operationLabel(operation) {
  return operation === 'edit' ? '图生图' : '文生图'
}

function resultLabel(item) {
  if (item.success) return '成功'
  if (item.error === 'No usable image data returned') return '无图片结果'
  return item.http_status == null ? '连接失败' : `HTTP ${item.http_status}`
}

function routeBadges(route) {
  const params = [...route.sizes.map((value) => `尺寸 ${value}`), ...route.qualities.map((value) => `质量 ${value}`)]
  return `
    <span>${escapeHtml(route.public_model)} → ${escapeHtml(route.upstream_model)}<small>${formatCost(route.cost_per_request)}</small></span>
    ${params.map((value) => `<span class="parameter-badge">${escapeHtml(value)}</span>`).join('')}`
}

function render() {
  for (const [key, value] of Object.entries(dashboard.stats)) {
    const node = document.querySelector(`#stat-${key}`)
    if (node) node.textContent = value
  }

  document.querySelector('#image-upstream-rows').innerHTML = dashboard.upstreams.map((upstream) => {
    const health = upstream.health || { state: 'unobserved', samples: 0, score: 0.9 }
    const rate = health.samples ? `评分 ${Math.round(health.score * 100)}% · ${health.samples} 次` : '无样本'
    return `
      <tr>
        <td><span class="state ${upstream.enabled ? 'enabled' : 'disabled'}">${upstream.enabled ? '启用' : '停用'}</span></td>
        <td><span class="health-state ${escapeHtml(health.state)}">${escapeHtml(healthLabel(health.state))}</span><small class="health-detail">${rate}</small></td>
        <td><strong>${escapeHtml(upstream.name)}</strong></td>
        <td><code>${escapeHtml(upstream.base_url)}</code></td>
        <td><div class="route-list image-route-list">${upstream.routes.map(routeBadges).join('')}</div></td>
        <td>${upstream.priority}</td>
        <td class="align-right"><button class="table-action" data-edit-image="${upstream.id}" type="button">编辑</button></td>
      </tr>`
  }).join('')
  document.querySelector('#image-upstream-empty').hidden = dashboard.upstreams.length > 0
  document.querySelector('#image-upstream-cards').innerHTML = dashboard.upstreams.map((upstream) => `
    <article class="mobile-item">
      <div class="mobile-item-heading">
        <div><strong>${escapeHtml(upstream.name)}</strong><span class="mobile-subtitle">优先级 ${upstream.priority}</span></div>
        <div class="mobile-item-actions"><span class="health-state ${escapeHtml(upstream.health.state)}">${escapeHtml(healthLabel(upstream.health.state))}</span><button class="table-action" data-edit-image="${upstream.id}" type="button">编辑</button></div>
      </div>
      <code class="mobile-url">${escapeHtml(upstream.base_url)}</code>
      <div class="route-list image-route-list">${upstream.routes.map(routeBadges).join('')}</div>
    </article>`).join('')

  document.querySelector('#image-log-rows').innerHTML = dashboard.requests.map((item) => `
    <tr>
      <td><code>${escapeHtml(item.request_id)}</code></td>
      <td>${escapeHtml(item.public_model)}</td>
      <td><strong>${escapeHtml(item.upstream_model)}</strong><small class="cell-detail">${escapeHtml(item.upstream_name)}</small></td>
      <td>${escapeHtml(item.size || '默认')} · ${escapeHtml(item.quality || '默认')}<small class="cell-detail">${operationLabel(item.operation)}</small></td>
      <td>${formatCost(item.cost_per_request)}</td>
      <td><span class="task-status ${item.success ? 'completed' : 'failed'}">${escapeHtml(resultLabel(item))}</span></td>
      <td>${item.latency_ms} ms</td>
      <td>${formatTime(item.created_at)}</td>
    </tr>`).join('')
  document.querySelector('#image-log-empty').hidden = dashboard.requests.length > 0
  document.querySelector('#image-log-cards').innerHTML = dashboard.requests.map((item) => `
    <article class="mobile-item task-item">
      <div class="mobile-item-heading"><code>${escapeHtml(item.request_id)}</code><span class="task-status ${item.success ? 'completed' : 'failed'}">${escapeHtml(resultLabel(item))}</span></div>
      <div class="task-summary"><strong>${escapeHtml(item.public_model)} → ${escapeHtml(item.upstream_model)}</strong><span>${escapeHtml(item.upstream_name)}</span></div>
      <div class="mobile-item-footer"><time>${formatTime(item.created_at)}</time><span>${formatCost(item.cost_per_request)} · ${item.latency_ms} ms</span></div>
    </article>`).join('')
}

async function loadDashboard() {
  dashboard = await api('/admin/api/images/dashboard')
  render()
}

async function loadLogs() {
  const params = new URLSearchParams()
  const query = document.querySelector('#image-log-search').value.trim()
  const outcome = document.querySelector('#image-log-outcome').value
  if (query) params.set('q', query)
  if (outcome) params.set('outcome', outcome)
  const result = await api(`/admin/api/images/requests?${params}`)
  dashboard.requests = result.requests
  render()
}

function operationMode(operations = ['generation']) {
  if (operations.includes('generation') && operations.includes('edit')) return 'both'
  return operations.includes('edit') ? 'edit' : 'generation'
}

function addRouteRow(route = {}) {
  const row = document.createElement('div')
  row.className = 'route-grid image-route-row'
  row.innerHTML = `
    <input data-route-field="public_model" required maxlength="160" value="${escapeHtml(route.public_model || '')}" placeholder="gpt-image-2" aria-label="公开模型">
    <input data-route-field="upstream_model" required maxlength="160" list="image-model-suggestions" value="${escapeHtml(route.upstream_model || '')}" placeholder="gpt-image-2-pro" aria-label="上游模型">
    <input data-route-field="sizes" required maxlength="300" value="${escapeHtml((route.sizes || ['*']).join(', '))}" placeholder="1k, 2k, 4k" aria-label="支持尺寸">
    <input data-route-field="qualities" required maxlength="300" value="${escapeHtml((route.qualities || ['*']).join(', '))}" placeholder="low, medium, high" aria-label="支持质量">
    <select data-route-field="operation" aria-label="支持接口">
      <option value="generation"${operationMode(route.operations) === 'generation' ? ' selected' : ''}>文生图</option>
      <option value="edit"${operationMode(route.operations) === 'edit' ? ' selected' : ''}>图生图</option>
      <option value="both"${operationMode(route.operations) === 'both' ? ' selected' : ''}>两者</option>
    </select>
    <input data-route-field="cost" type="number" min="0" max="100000" step="0.000001" required value="${Number(route.cost_per_request || 0)}" aria-label="每次成本">
    <button class="route-remove" type="button" aria-label="删除路由">×</button>`
  routeRows.appendChild(row)
}

function closeDialog() {
  dialog.close()
  form.reset()
  routeRows.innerHTML = ''
}

function openDialog(upstream = null) {
  document.querySelector('#image-dialog-title').textContent = upstream ? '编辑画图上游' : '添加画图上游'
  document.querySelector('#image-upstream-id').value = upstream?.id || ''
  document.querySelector('#image-upstream-name').value = upstream?.name || ''
  document.querySelector('#image-upstream-priority').value = upstream?.priority ?? 100
  document.querySelector('#image-upstream-base-url').value = upstream?.base_url || ''
  document.querySelector('#image-upstream-api-key').value = ''
  document.querySelector('#image-upstream-enabled').checked = upstream?.enabled ?? true
  document.querySelector('#delete-image-upstream').hidden = !upstream
  document.querySelector('#image-form-error').hidden = true
  routeRows.innerHTML = ''
  for (const route of upstream?.routes || [{}]) addRouteRow(route)
  dialog.showModal()
  document.querySelector('#image-upstream-name').focus()
}

function parseList(value) {
  const result = [...new Set(value.split(',').map((item) => item.trim().toLowerCase()).filter(Boolean))]
  return result.length ? result : ['*']
}

function collectRoutes() {
  return [...document.querySelectorAll('.image-route-row')].map((row) => {
    const mode = row.querySelector('[data-route-field="operation"]').value
    return {
      public_model: row.querySelector('[data-route-field="public_model"]').value.trim(),
      upstream_model: row.querySelector('[data-route-field="upstream_model"]').value.trim(),
      sizes: parseList(row.querySelector('[data-route-field="sizes"]').value),
      qualities: parseList(row.querySelector('[data-route-field="qualities"]').value),
      operations: mode === 'both' ? ['generation', 'edit'] : [mode],
      cost_per_request: Number(row.querySelector('[data-route-field="cost"]').value),
    }
  })
}

document.querySelector('#add-image-upstream').addEventListener('click', () => openDialog())
document.querySelector('#add-image-route').addEventListener('click', () => addRouteRow())
document.querySelector('#close-image-dialog').addEventListener('click', closeDialog)
document.querySelector('#cancel-image-dialog').addEventListener('click', closeDialog)
dialog.addEventListener('click', (event) => { if (event.target === dialog) closeDialog() })
routeRows.addEventListener('click', (event) => {
  if (!event.target.classList.contains('route-remove')) return
  event.target.closest('.image-route-row').remove()
  if (!routeRows.children.length) addRouteRow()
})

document.querySelector('#image-upstream-rows').addEventListener('click', (event) => {
  const id = Number(event.target.dataset.editImage)
  if (id) openDialog(dashboard.upstreams.find((item) => item.id === id))
})
document.querySelector('#image-upstream-cards').addEventListener('click', (event) => {
  const id = Number(event.target.dataset.editImage)
  if (id) openDialog(dashboard.upstreams.find((item) => item.id === id))
})

document.querySelector('#refresh-image-button').addEventListener('click', async () => {
  try {
    await loadDashboard()
    showToast('数据已刷新')
  } catch (error) {
    showToast(error.message, 'error')
  }
})

document.querySelector('#image-log-filter').addEventListener('submit', async (event) => {
  event.preventDefault()
  try { await loadLogs() } catch (error) { showToast(error.message, 'error') }
})

document.querySelector('#discover-image-models').addEventListener('click', async (event) => {
  const button = event.currentTarget
  const baseUrl = document.querySelector('#image-upstream-base-url').value.trim()
  if (!baseUrl) {
    document.querySelector('#image-form-error').textContent = '请先填写 Base URL'
    document.querySelector('#image-form-error').hidden = false
    return
  }
  button.disabled = true
  button.textContent = '获取中...'
  try {
    const upstreamId = document.querySelector('#image-upstream-id').value
    const result = await api('/admin/api/images/upstreams/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upstream_id: upstreamId ? Number(upstreamId) : null,
        base_url: baseUrl,
        api_key: document.querySelector('#image-upstream-api-key').value,
      }),
    })
    document.querySelector('#image-model-suggestions').innerHTML = result.models.map((model) => `<option value="${escapeHtml(model)}"></option>`).join('')
    showToast(`已获取 ${result.models.length} 个上游模型`)
  } catch (error) {
    document.querySelector('#image-form-error').textContent = error.message
    document.querySelector('#image-form-error').hidden = false
  } finally {
    button.disabled = false
    button.textContent = '获取模型'
  }
})

form.addEventListener('submit', async (event) => {
  event.preventDefault()
  const errorBox = document.querySelector('#image-form-error')
  errorBox.hidden = true
  try {
    const id = document.querySelector('#image-upstream-id').value
    const routes = collectRoutes()
    if (!routes.length || routes.some((route) => !route.public_model || !route.upstream_model)) {
      throw new Error('请完整填写至少一条参数路由')
    }
    await api(id ? `/admin/api/images/upstreams/${id}` : '/admin/api/images/upstreams', {
      method: id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: document.querySelector('#image-upstream-name').value,
        priority: Number(document.querySelector('#image-upstream-priority').value),
        base_url: document.querySelector('#image-upstream-base-url').value,
        api_key: document.querySelector('#image-upstream-api-key').value,
        enabled: document.querySelector('#image-upstream-enabled').checked,
        routes,
      }),
    })
    closeDialog()
    await loadDashboard()
    showToast('画图上游已保存')
  } catch (error) {
    errorBox.textContent = error.message
    errorBox.hidden = false
  }
})

document.querySelector('#delete-image-upstream').addEventListener('click', async () => {
  const id = document.querySelector('#image-upstream-id').value
  if (!id || !window.confirm('确认删除这个画图上游？')) return
  try {
    await api(`/admin/api/images/upstreams/${id}`, { method: 'DELETE' })
    closeDialog()
    await loadDashboard()
    showToast('画图上游已删除')
  } catch (error) {
    document.querySelector('#image-form-error').textContent = error.message
    document.querySelector('#image-form-error').hidden = false
  }
})

document.querySelector('#logout-button').addEventListener('click', async () => {
  await api('/admin/api/logout', { method: 'POST' })
  window.location.assign('/admin/login')
})

loadDashboard().catch((error) => showToast(error.message, 'error'))
