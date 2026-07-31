const csrf = document.querySelector('meta[name="csrf-token"]').content
const dialog = document.querySelector('#upstream-dialog')
const form = document.querySelector('#upstream-form')
const toast = document.querySelector('#toast')
const discoverModelsButton = document.querySelector('#discover-models')
const addRouteButton = document.querySelector('#add-route')
const routeRows = document.querySelector('#route-rows')
const routeEmpty = document.querySelector('#route-empty')
const auditDialog = document.querySelector('#audit-dialog')
const auditVideo = document.querySelector('#audit-video')
const auditEvent = document.querySelector('#audit-event')
const auditJson = document.querySelector('#audit-json')
const copyAuditJsonButton = document.querySelector('#copy-audit-json')
const DURATION_OPTIONS = [4, 5, 8, 10, 12, 15]
const publicLinkForm = document.querySelector('#public-link-form')
const publicLinkBaseUrl = document.querySelector('#public-link-base-url')
let dashboard = { upstreams: [], tasks: [], stats: {} }
let activeAudit = null
let activeAuditView = 'request'
let activeDurationMenu = null

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

function profileLabel(profile) {
  return dashboard.profiles?.find((item) => item.id === profile)?.label || profile
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

async function copyText(value) {
  if (navigator.clipboard && window.isSecureContext) {
    await navigator.clipboard.writeText(value)
    return
  }
  const textarea = document.createElement('textarea')
  textarea.value = value
  textarea.setAttribute('readonly', '')
  textarea.style.position = 'fixed'
  textarea.style.opacity = '0'
  document.body.appendChild(textarea)
  textarea.select()
  const copied = document.execCommand('copy')
  textarea.remove()
  if (!copied) throw new Error('复制失败')
}

function render() {
  for (const [key, value] of Object.entries(dashboard.stats)) {
    const node = document.querySelector(`#stat-${key}`)
    if (node) node.textContent = value
  }

  publicLinkBaseUrl.innerHTML = (dashboard.public_link_base_url_options || [])
    .map((url) => `<option value="${escapeHtml(url)}">${escapeHtml(url)}</option>`)
    .join('')
  publicLinkBaseUrl.value = dashboard.public_link_base_url || 'https://zl.yyapi.cloud'

  const upstreamRows = document.querySelector('#upstream-rows')
  upstreamRows.innerHTML = dashboard.upstreams.map((upstream) => `
    <tr>
      <td><span class="state ${upstream.enabled ? 'enabled' : 'disabled'}">${upstream.enabled ? '启用' : '停用'}</span></td>
      <td><strong>${escapeHtml(upstream.name)}</strong></td>
      <td><code>${escapeHtml(upstream.base_url)}</code></td>
      <td><div class="route-list">${upstream.routes.map((route) => `<span>${escapeHtml(route.model)}${route.mapped_upstream_model ? `<b>→ ${escapeHtml(route.upstream_model)}</b>` : ''}<small>${route.protocol} · ${escapeHtml(profileLabel(route.profile))}${route.durations?.length ? ` · ${route.durations.map((duration) => `${duration}s`).join(', ')}` : ''}</small></span>`).join('')}</div></td>
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
      <div class="route-list">${upstream.routes.map((route) => `<span>${escapeHtml(route.model)}${route.mapped_upstream_model ? `<b>→ ${escapeHtml(route.upstream_model)}</b>` : ''}<small>${route.protocol} · ${escapeHtml(profileLabel(route.profile))}${route.durations?.length ? ` · ${route.durations.map((duration) => `${duration}s`).join(', ')}` : ''}</small></span>`).join('')}</div>
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
  copyAuditJsonButton.disabled = !auditJson.textContent
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

function updateRouteEmpty() {
  routeEmpty.hidden = routeRows.children.length > 0
}

function routeDurations(route) {
  const values = Array.isArray(route.durations)
    ? route.durations
    : (route.duration_override ? [route.duration_override] : [])
  return DURATION_OPTIONS.filter((duration) => values.includes(duration))
}

function updateDurationSummary(row) {
  const values = JSON.parse(row.dataset.durations || '[]').map((duration) => `${duration}s`)
  row.querySelector('[data-duration-summary]').textContent = values.length ? values.join(', ') : '工作台默认'
}

function closeDurationMenu() {
  if (!activeDurationMenu) return
  activeDurationMenu.menu.remove()
  activeDurationMenu = null
}

function openDurationMenu(row, trigger) {
  closeDurationMenu()
  const menu = document.createElement('div')
  menu.className = 'duration-menu'
  const selected = JSON.parse(row.dataset.durations || '[]')
  menu.innerHTML = DURATION_OPTIONS.map((duration) => `
    <label><input data-menu-duration type="checkbox" value="${duration}"${selected.includes(duration) ? ' checked' : ''}>${duration}s</label>`).join('')
  dialog.appendChild(menu)
  const rect = trigger.getBoundingClientRect()
  const menuRect = menu.getBoundingClientRect()
  const left = Math.min(Math.max(8, rect.left), window.innerWidth - menuRect.width - 8)
  const below = rect.bottom + 4
  const top = below + menuRect.height <= window.innerHeight - 8
    ? below
    : Math.max(8, rect.top - menuRect.height - 4)
  menu.style.left = `${left}px`
  menu.style.top = `${top}px`
  activeDurationMenu = { menu, row }
  menu.addEventListener('change', (event) => {
    if (!event.target.matches('[data-menu-duration]')) return
    const durations = [...menu.querySelectorAll('[data-menu-duration]:checked')].map((input) => Number(input.value))
    row.dataset.durations = JSON.stringify(durations)
    updateDurationSummary(row)
  })
}

function addRouteRow(route = {}) {
  const row = document.createElement('div')
  row.className = 'route-editor-row'
  const mappedUpstreamModel = route.upstream_model || route.mapped_upstream_model || ''
  const selectedDurations = routeDurations(route)
  row.dataset.durations = JSON.stringify(selectedDurations)
  row.dataset.profile = route.profile || 'default'
  row.innerHTML = `
    <input data-route-field="model" maxlength="160" value="${escapeHtml(route.model || '')}" placeholder="对外模型名" aria-label="对外模型名">
    <select data-route-field="protocol" aria-label="请求类型">
      <option value="videos"${route.protocol !== 'seedance' ? ' selected' : ''}>videos</option>
      <option value="seedance"${route.protocol === 'seedance' ? ' selected' : ''}>seedance</option>
    </select>
    <input data-route-field="upstream_model" maxlength="160" value="${escapeHtml(mappedUpstreamModel)}" placeholder="上游模型名" aria-label="映射上游模型名">
    <button class="duration-picker" data-duration-trigger data-duration-summary type="button">${selectedDurations.length ? selectedDurations.map((duration) => `${duration}s`).join(', ') : '工作台默认'}</button>
    <div class="media-support-options" aria-label="素材支持">
      <label><input data-route-support="image" type="checkbox"${route.supports_image !== false ? ' checked' : ''}>图片</label>
      <label><input data-route-support="video" type="checkbox"${route.supports_video !== false ? ' checked' : ''}>视频</label>
      <label><input data-route-support="audio" type="checkbox"${route.supports_audio !== false ? ' checked' : ''}>音频</label>
    </div>
    <button class="route-remove" type="button" title="移除此模型" aria-label="移除此模型">×</button>`
  routeRows.appendChild(row)
  updateRouteEmpty()
}

function setRouteRows(routes) {
  closeDurationMenu()
  routeRows.innerHTML = ''
  for (const route of routes) addRouteRow(route)
  updateRouteEmpty()
}

function readRoutes(allowEmpty = false, preserveBlankModel = false) {
  const routes = [...routeRows.children].map((row) => {
    const model = row.querySelector('[data-route-field="model"]').value.trim()
    const upstreamModel = row.querySelector('[data-route-field="upstream_model"]').value.trim()
    const durations = JSON.parse(row.dataset.durations || '[]')
    const effectiveModel = model || upstreamModel
    if (!effectiveModel) throw new Error('每一行都必须填写对外模型名或映射上游模型名')
    return {
      model: preserveBlankModel ? model : effectiveModel,
      upstream_model: upstreamModel,
      protocol: row.querySelector('[data-route-field="protocol"]').value,
      profile: row.dataset.profile || 'default',
      durations,
      duration_override: durations.length === 1 ? durations[0] : null,
      supports_image: row.querySelector('[data-route-support="image"]').checked,
      supports_video: row.querySelector('[data-route-support="video"]').checked,
      supports_audio: row.querySelector('[data-route-support="audio"]').checked,
    }
  })
  if (!routes.length && !allowEmpty) throw new Error('至少需要一个模型路由')
  const publicModels = routes.map((route) => route.model || route.upstream_model)
  if (new Set(publicModels).size !== publicModels.length) throw new Error('对外模型名不能重复')
  const upstreamModels = routes.map((route) => route.upstream_model || route.model)
  if (new Set(upstreamModels).size !== upstreamModels.length) throw new Error('同一个上游模型不能重复映射')
  return routes
}

function openDialog(upstream = null) {
  document.querySelector('#dialog-title').textContent = upstream ? '编辑上游' : '添加上游'
  document.querySelector('#upstream-id').value = upstream?.id || ''
  document.querySelector('#upstream-name').value = upstream?.name || ''
  document.querySelector('#upstream-priority').value = upstream?.priority ?? 100
  document.querySelector('#upstream-base-url').value = upstream?.base_url || 'https://pidoi.com'
  document.querySelector('#upstream-api-key').value = ''
  setRouteRows(upstream?.routes || [])
  document.querySelector('#upstream-enabled').checked = upstream?.enabled ?? true
  document.querySelector('#delete-upstream').hidden = !upstream
  document.querySelector('#form-error').hidden = true
  dialog.showModal()
  document.querySelector('#upstream-name').focus()
}

function closeDialog() {
  closeDurationMenu()
  dialog.close()
  form.reset()
  setRouteRows([])
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
    const currentRoutes = readRoutes(true, true)
    const currentByUpstream = new Map(currentRoutes.map((route) => [route.upstream_model || route.model, route]))
    setRouteRows(result.models.map((item) => currentByUpstream.get(item.upstream_model) || item))
    showToast(`已同步 ${result.models.length} 个上游模型`)
  } catch (error) {
    document.querySelector('#form-error').textContent = error.message
    document.querySelector('#form-error').hidden = false
  } finally {
    discoverModelsButton.disabled = false
    discoverModelsButton.textContent = '同步上游模型'
  }

})

addRouteButton.addEventListener('click', () => addRouteRow())
routeRows.addEventListener('click', (event) => {
  const target = event.target instanceof Element ? event.target : null
  if (target?.matches('[data-duration-trigger]')) {
    openDurationMenu(target.closest('.route-editor-row'), target)
    return
  }
  if (!target?.classList.contains('route-remove')) return
  closeDurationMenu()
  target.closest('.route-editor-row').remove()
  updateRouteEmpty()
})
routeRows.addEventListener('change', (event) => {
  const target = event.target instanceof Element ? event.target : null
  if (!target?.matches('[data-route-field="protocol"]')) return
  const row = target.closest('.route-editor-row')
  if (row && target.value === 'seedance') row.dataset.profile = 'default'
})
document.addEventListener('click', (event) => {
  if (!activeDurationMenu) return
  const target = event.target instanceof Node ? event.target : null
  if (activeDurationMenu.menu.contains(target) || (target instanceof Element && target.matches('[data-duration-trigger]'))) return
  closeDurationMenu()
})
window.addEventListener('resize', closeDurationMenu)

document.querySelector('#add-upstream').addEventListener('click', () => openDialog())
document.querySelector('#close-dialog').addEventListener('click', closeDialog)
document.querySelector('#cancel-dialog').addEventListener('click', closeDialog)
document.querySelector('#refresh-button').addEventListener('click', async () => {
  await loadDashboard()
  showToast('数据已刷新')
})
publicLinkForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  const button = publicLinkForm.querySelector('button[type="submit"]')
  button.disabled = true
  try {
    const result = await api('/admin/api/settings/public-link', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ public_base_url: publicLinkBaseUrl.value }),
    })
    dashboard.public_link_base_url = result.public_link_base_url
    showToast('返回域名已保存')
  } catch (error) {
    showToast(error.message, 'error')
  } finally {
    button.disabled = false
  }
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
    try {
      await copyText(value)
      showToast('已复制')
    } catch (error) {
      showToast(error.message, 'error')
    }
  })
})
copyAuditJsonButton.addEventListener('click', async () => {
  const value = auditJson.textContent
  if (!value || copyAuditJsonButton.disabled) return
  try {
    await copyText(value)
    showToast('已复制全部内容')
  } catch (error) {
    showToast(error.message, 'error')
  }
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
      routes: readRoutes(),
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
