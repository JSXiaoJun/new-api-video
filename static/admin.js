const csrf = document.querySelector('meta[name="csrf-token"]').content
const dialog = document.querySelector('#upstream-dialog')
const form = document.querySelector('#upstream-form')
const toast = document.querySelector('#toast')
const discoverModelsButton = document.querySelector('#discover-models')
const modelPicker = document.querySelector('#model-picker')
const modelOptions = document.querySelector('#model-options')
const modelPickerStatus = document.querySelector('#model-picker-status')
const applyModelsButton = document.querySelector('#apply-models')
const auditDialog = document.querySelector('#audit-dialog')
const auditVideo = document.querySelector('#audit-video')
const auditEvent = document.querySelector('#audit-event')
const auditJson = document.querySelector('#audit-json')
let dashboard = { upstreams: [], tasks: [], stats: {} }
let activeAudit = null
let activeAuditView = 'request'

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
    throw new Error(payload.detail || `请求失败 (${response.status})`)
  }
  return response.json()
}

function statusLabel(status) {
  const labels = { queued: '排队中', processing: '处理中', completed: '已完成', failed: '失败' }
  return labels[status] || status
}

function formatTime(timestamp) {
  if (!timestamp) return '—'
  return new Intl.DateTimeFormat('zh-CN', { dateStyle: 'short', timeStyle: 'medium' }).format(new Date(timestamp * 1000))
}

function formatJson(value) {
  if (value === null || value === undefined || value === '') return '无记录'
  if (typeof value === 'string') {
    try { return JSON.stringify(JSON.parse(value), null, 2) } catch { return value }
  }
  return JSON.stringify(value, null, 2)
}

function render() {
  for (const [key, value] of Object.entries(dashboard.stats)) {
    const node = document.querySelector(`#stat-${key}`)
    if (node) node.textContent = value
  }

  const upstreamRows = document.querySelector('#upstream-rows')
  upstreamRows.innerHTML = dashboard.upstreams.map((upstream) => `
    <tr>
      <td><span class="state ${upstream.enabled ? 'enabled' : 'disabled'}">${upstream.enabled ? '启用' : '停用'}</span></td>
      <td><strong>${escapeHtml(upstream.name)}</strong></td>
      <td><code>${escapeHtml(upstream.base_url)}</code></td>
      <td><div class="route-list">${upstream.routes.map((route) => `<span>${escapeHtml(route.model)}<small>${route.protocol}</small></span>`).join('')}</div></td>
      <td>${upstream.priority}</td>
      <td class="align-right"><button class="table-action" data-edit="${upstream.id}" type="button">编辑</button></td>
    </tr>`).join('')
  document.querySelector('#upstream-empty').hidden = dashboard.upstreams.length > 0
  document.querySelector('#upstream-cards').innerHTML = dashboard.upstreams.map((upstream) => `
    <article class="mobile-item">
      <div class="mobile-item-heading">
        <div><strong>${escapeHtml(upstream.name)}</strong><span class="mobile-subtitle">优先级 ${upstream.priority}</span></div>
        <div class="mobile-item-actions"><span class="state ${upstream.enabled ? 'enabled' : 'disabled'}">${upstream.enabled ? '启用' : '停用'}</span><button class="table-action" data-edit="${upstream.id}" type="button">编辑</button></div>
      </div>
      <code class="mobile-url">${escapeHtml(upstream.base_url)}</code>
      <div class="route-list">${upstream.routes.map((route) => `<span>${escapeHtml(route.model)}<small>${route.protocol}</small></span>`).join('')}</div>
    </article>`).join('')

  const taskRows = document.querySelector('#task-rows')
  taskRows.innerHTML = dashboard.tasks.map((task) => `
    <tr>
      <td><code>${escapeHtml(task.relay_request_id)}</code></td>
      <td>${escapeHtml(task.model)}</td>
      <td>${escapeHtml(task.upstream_name)}</td>
      <td><span class="task-status ${escapeHtml(task.status)}">${escapeHtml(statusLabel(task.status))}</span></td>
      <td>${formatTime(task.created_at)}</td>
      <td class="align-right"><button class="table-action" data-audit="${escapeHtml(task.relay_request_id)}" type="button">详情</button></td>
    </tr>`).join('')
  document.querySelector('#task-empty').hidden = dashboard.tasks.length > 0
  document.querySelector('#task-cards').innerHTML = dashboard.tasks.map((task) => `
    <article class="mobile-item task-item">
      <div class="mobile-item-heading"><code>${escapeHtml(task.relay_request_id)}</code><span class="task-status ${escapeHtml(task.status)}">${escapeHtml(statusLabel(task.status))}</span></div>
      <div class="task-summary"><strong>${escapeHtml(task.model)}</strong><span>${escapeHtml(task.upstream_name)}</span></div>
      <div class="mobile-item-footer"><time>${formatTime(task.created_at)}</time><button class="table-action" data-audit="${escapeHtml(task.relay_request_id)}" type="button">详情</button></div>
    </article>`).join('')
}

async function loadDashboard() {
  dashboard = await api('/admin/api/dashboard')
  render()
}

async function loadTasks() {
  const params = new URLSearchParams()
  const query = document.querySelector('#task-search').value.trim()
  const status = document.querySelector('#task-status').value
  if (query) params.set('q', query)
  if (status) params.set('status', status)
  const result = await api(`/admin/api/tasks?${params}`)
  dashboard.tasks = result.tasks
  render()
}

function selectedAuditEvent() {
  if (!activeAudit) return null
  const eventId = Number(auditEvent.value)
  return activeAudit.events.find((event) => event.id === eventId) || activeAudit.events[0] || null
}

function renderAuditJson() {
  const event = selectedAuditEvent()
  if (activeAuditView === 'request') {
    auditJson.textContent = formatJson(activeAudit?.request_payload)
  } else if (activeAuditView === 'upstream') {
    auditJson.textContent = formatJson(event?.upstream_body)
  } else {
    auditJson.textContent = formatJson(event?.sanitized_body)
  }
}

function renderAuditDetail(task) {
  activeAudit = task
  document.querySelector('#audit-relay-id').textContent = task.relay_request_id
  document.querySelector('#audit-upstream-id').textContent = task.upstream_task_id || '尚未返回'
  document.querySelector('#audit-model').textContent = task.model
  document.querySelector('#audit-upstream').textContent = task.upstream_name
  document.querySelector('#audit-created').textContent = formatTime(task.created_at)
  document.querySelector('#audit-status').innerHTML = `<span class="task-status ${escapeHtml(task.status)}">${escapeHtml(statusLabel(task.status))}</span>`

  const sourceUrl = task.source_video_url || ''
  document.querySelector('#audit-source-url').textContent = sourceUrl || '尚未返回'
  const sourceOpen = document.querySelector('#audit-source-open')
  sourceOpen.hidden = !sourceUrl
  sourceOpen.href = sourceUrl || '#'
  document.querySelector('[data-copy-target="audit-source-url"]').disabled = !sourceUrl

  document.querySelector('#public-task-id').value = task.public_task_id || ''
  document.querySelector('#audit-public-url').textContent = task.sanitized_video_url || '填写公开任务 ID 后生成'
  document.querySelector('[data-copy-target="audit-public-url"]').disabled = !task.sanitized_video_url

  const videoAvailable = task.status === 'completed' && task.upstream_task_id
  auditVideo.hidden = !videoAvailable
  if (videoAvailable) {
    auditVideo.src = `/admin/api/tasks/${encodeURIComponent(task.relay_request_id)}/content`
    auditVideo.load()
  } else {
    auditVideo.removeAttribute('src')
    auditVideo.load()
  }

  auditEvent.innerHTML = task.events.map((event) => `
    <option value="${event.id}">${event.phase === 'create' ? '创建' : '轮询'} · HTTP ${event.http_status ?? '连接失败'} · ${formatTime(event.created_at)}</option>
  `).join('') || '<option value="">暂无事件</option>'
  activeAuditView = 'request'
  for (const tab of document.querySelectorAll('.audit-tab')) {
    const active = tab.dataset.auditView === activeAuditView
    tab.classList.toggle('active', active)
    tab.setAttribute('aria-selected', String(active))
  }
  renderAuditJson()
}

async function openAuditDialog(relayRequestId) {
  document.querySelector('#audit-loading').hidden = false
  document.querySelector('#audit-content').hidden = true
  auditDialog.showModal()
  try {
    const task = await api(`/admin/api/tasks/${encodeURIComponent(relayRequestId)}`)
    renderAuditDetail(task)
    document.querySelector('#audit-loading').hidden = true
    document.querySelector('#audit-content').hidden = false
  } catch (error) {
    auditDialog.close()
    showToast(error.message, 'error')
  }
}

function closeAuditDialog() {
  auditVideo.pause()
  auditVideo.removeAttribute('src')
  auditVideo.load()
  activeAudit = null
  auditDialog.close()
}

function parseRoutes(value) {
  const routes = value.split('\n').map((line) => line.trim()).filter(Boolean).map((line) => {
    const [modelPart, protocolPart] = line.split('|').map((part) => part.trim())
    const protocol = protocolPart || (modelPart === 'seedance-2.0-fast' ? 'seedance' : 'videos')
    if (!['videos', 'seedance'].includes(protocol)) throw new Error(`未知协议：${protocol}`)
    return { model: modelPart, protocol }
  })
  if (!routes.length || routes.some((route) => !route.model)) throw new Error('至少需要一个模型路由')
  return routes
}

function openDialog(upstream = null) {
  document.querySelector('#dialog-title').textContent = upstream ? '编辑上游' : '添加上游'
  document.querySelector('#upstream-id').value = upstream?.id || ''
  document.querySelector('#upstream-name').value = upstream?.name || ''
  document.querySelector('#upstream-priority').value = upstream?.priority ?? 100
  document.querySelector('#upstream-base-url').value = upstream?.base_url || 'https://pidoi.com'
  document.querySelector('#upstream-api-key').value = ''
  document.querySelector('#upstream-routes').value = upstream?.routes.map((route) => `${route.model} | ${route.protocol}`).join('\n') || ''
  document.querySelector('#upstream-enabled').checked = upstream?.enabled ?? true
  document.querySelector('#delete-upstream').hidden = !upstream
  document.querySelector('#form-error').hidden = true
  modelPicker.hidden = true
  modelOptions.innerHTML = ''
  modelPickerStatus.textContent = ''
  applyModelsButton.disabled = true
  modelPicker._models = []
  dialog.showModal()
  document.querySelector('#upstream-name').focus()
}

function closeDialog() {
  dialog.close()
  form.reset()
  modelPicker.hidden = true
  modelOptions.innerHTML = ''
  modelPicker._models = []
}

function renderModelOptions(models) {
  modelOptions.innerHTML = models.map((item, index) => `
    <label class="model-option">
      <input type="checkbox" data-model-index="${index}">
      <span class="model-option-name">${escapeHtml(item.model)}</span>
      <select data-protocol-index="${index}" aria-label="${escapeHtml(item.model)} 协议">
        <option value="videos"${item.protocol === 'videos' ? ' selected' : ''}>videos</option>
        <option value="seedance"${item.protocol === 'seedance' ? ' selected' : ''}>seedance</option>
      </select>
    </label>`).join('')
  modelPicker.hidden = false
  modelPickerStatus.textContent = `${models.length} 个模型`
  applyModelsButton.disabled = true
  for (const checkbox of modelOptions.querySelectorAll('input[type="checkbox"]')) {
    checkbox.addEventListener('change', () => {
      applyModelsButton.disabled = !modelOptions.querySelector('input[type="checkbox"]:checked')
    })
  }
  modelPicker._models = models
}

discoverModelsButton.addEventListener('click', async () => {
  const baseUrl = document.querySelector('#upstream-base-url').value.trim()
  const apiKey = document.querySelector('#upstream-api-key').value
  const upstreamId = document.querySelector('#upstream-id').value
  if (!baseUrl) {
    document.querySelector('#form-error').textContent = '请先填写 Base URL'
    document.querySelector('#form-error').hidden = false
    return
  }
  discoverModelsButton.disabled = true
  discoverModelsButton.textContent = '获取中...'
  try {
    const result = await api('/admin/api/upstreams/models', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        upstream_id: upstreamId ? Number(upstreamId) : null,
        base_url: baseUrl,
        api_key: apiKey,
      }),
    })
    renderModelOptions(result.models)
    showToast(`已获取 ${result.models.length} 个上游模型`)
  } catch (error) {
    modelPicker.hidden = true
    document.querySelector('#form-error').textContent = error.message
    document.querySelector('#form-error').hidden = false
  } finally {
    discoverModelsButton.disabled = false
    discoverModelsButton.textContent = '获取上游模型'
  }
})

applyModelsButton.addEventListener('click', () => {
  const models = modelPicker._models || []
  const routes = new Map()
  try {
    const currentText = document.querySelector('#upstream-routes').value.trim()
    for (const route of (currentText ? parseRoutes(currentText) : [])) routes.set(route.model, route)
    for (const checkbox of modelOptions.querySelectorAll('input[type="checkbox"]:checked')) {
      const index = Number(checkbox.dataset.modelIndex)
      const model = models[index]
      routes.set(model.model, { model: model.model, protocol: modelOptions.querySelector(`[data-protocol-index="${index}"]`).value })
    }
  } catch (error) {
    document.querySelector('#form-error').textContent = error.message
    document.querySelector('#form-error').hidden = false
    return
  }
  document.querySelector('#upstream-routes').value = [...routes.values()].map((route) => `${route.model} | ${route.protocol}`).join('\n')
  modelPicker.hidden = true
  showToast('已将所选模型加入路由')
})

document.querySelector('#add-upstream').addEventListener('click', () => openDialog())
document.querySelector('#close-dialog').addEventListener('click', closeDialog)
document.querySelector('#cancel-dialog').addEventListener('click', closeDialog)
document.querySelector('#refresh-button').addEventListener('click', async () => {
  await loadDashboard()
  showToast('数据已刷新')
})
document.querySelector('#task-filter').addEventListener('submit', async (event) => {
  event.preventDefault()
  try { await loadTasks() } catch (error) { showToast(error.message, 'error') }
})
document.querySelector('#task-rows').addEventListener('click', (event) => {
  if (event.target.dataset.audit) openAuditDialog(event.target.dataset.audit)
})
document.querySelector('#task-cards').addEventListener('click', (event) => {
  if (event.target.dataset.audit) openAuditDialog(event.target.dataset.audit)
})
document.querySelector('#upstream-rows').addEventListener('click', (event) => {
  const id = Number(event.target.dataset.edit)
  if (id) openDialog(dashboard.upstreams.find((item) => item.id === id))
})
dialog.addEventListener('click', (event) => {
  if (event.target === dialog) closeDialog()
})
document.querySelector('#close-audit').addEventListener('click', closeAuditDialog)
auditDialog.addEventListener('click', (event) => {
  if (event.target === auditDialog) closeAuditDialog()
})
auditEvent.addEventListener('change', renderAuditJson)
document.querySelector('.audit-tabs').addEventListener('click', (event) => {
  const view = event.target.dataset.auditView
  if (!view) return
  activeAuditView = view
  for (const tab of document.querySelectorAll('.audit-tab')) {
    const active = tab.dataset.auditView === view
    tab.classList.toggle('active', active)
    tab.setAttribute('aria-selected', String(active))
  }
  renderAuditJson()
})
document.querySelector('#public-task-form').addEventListener('submit', async (event) => {
  event.preventDefault()
  if (!activeAudit) return
  try {
    const task = await api(`/admin/api/tasks/${encodeURIComponent(activeAudit.relay_request_id)}/public-task`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ public_task_id: document.querySelector('#public-task-id').value.trim() }),
    })
    renderAuditDetail(task)
    showToast('公开任务 ID 已保存')
  } catch (error) {
    showToast(error.message, 'error')
  }
})
document.querySelectorAll('.copy-button').forEach((button) => {
  button.addEventListener('click', async () => {
    const value = document.querySelector(`#${button.dataset.copyTarget}`).textContent
    if (!value || button.disabled) return
    await navigator.clipboard.writeText(value)
    showToast('已复制')
  })
})

form.addEventListener('submit', async (event) => {
  event.preventDefault()
  const errorBox = document.querySelector('#form-error')
  errorBox.hidden = true
  try {
    const id = document.querySelector('#upstream-id').value
    const payload = {
      name: document.querySelector('#upstream-name').value,
      priority: Number(document.querySelector('#upstream-priority').value),
      base_url: document.querySelector('#upstream-base-url').value,
      api_key: document.querySelector('#upstream-api-key').value,
      enabled: document.querySelector('#upstream-enabled').checked,
      routes: parseRoutes(document.querySelector('#upstream-routes').value),
    }
    await api(id ? `/admin/api/upstreams/${id}` : '/admin/api/upstreams', {
      method: id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
    closeDialog()
    await loadDashboard()
    showToast('上游已保存')
  } catch (error) {
    errorBox.textContent = error.message
    errorBox.hidden = false
  }
})

document.querySelector('#delete-upstream').addEventListener('click', async () => {
  const id = document.querySelector('#upstream-id').value
  if (!id || !window.confirm('确认删除这个上游？')) return
  try {
    await api(`/admin/api/upstreams/${id}`, { method: 'DELETE' })
    closeDialog()
    await loadDashboard()
    showToast('上游已删除')
  } catch (error) {
    document.querySelector('#form-error').textContent = error.message
    document.querySelector('#form-error').hidden = false
  }
})

document.querySelector('#logout-button').addEventListener('click', async () => {
  await api('/admin/api/logout', { method: 'POST' })
  window.location.assign('/admin/login')
})

loadDashboard().catch((error) => showToast(error.message, 'error'))
