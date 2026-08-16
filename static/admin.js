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
const MAX_DURATION_SECONDS = 60
const publicLinkForm = document.querySelector('#public-link-form')
const publicLinkBaseUrl = document.querySelector('#public-link-base-url')
const publicVideoSettingsForm = document.querySelector('#public-video-settings-form')
const publicVideoDownloadLimit = document.querySelector('#public-video-download-limit')
const integrationDocumentButton = document.querySelector('#integration-document-button')
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

async function downloadIntegrationDocument(event) {
  event.preventDefault()
  integrationDocumentButton.setAttribute('aria-disabled', 'true')
  try {
    const response = await fetch(integrationDocumentButton.href)
    if (response.status === 401) {
      window.location.assign('/admin/login')
      return
    }
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || `生成失败 (${response.status})`)
    }
    const blob = await response.blob()
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = 'video-api-integration.md'
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
    showToast('对接文档已生成')
  } catch (error) {
    showToast(error instanceof Error ? error.message : '生成对接文档失败', 'error')
  } finally {
    integrationDocumentButton.removeAttribute('aria-disabled')
  }
}

function profileOptions(selected) {
  return (dashboard.profiles || [])
    .map((profile) => `<option value="${escapeHtml(profile.id)}"${profile.id === selected ? ' selected' : ''}>${escapeHtml(profile.label)}</option>`)
    .join('')
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
  publicVideoDownloadLimit.value = dashboard.public_video_download_limit || 50

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
  } else if (activeAuditView === 'transformed') {
    auditJson.textContent = formatJson(activeAudit?.upstream_request_payload)
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
  return [...new Set(values.map(Number))]
    .filter((duration) => Number.isInteger(duration) && duration >= 1 && duration <= MAX_DURATION_SECONDS)
    .sort((a, b) => a - b)
}

function durationRanges(values) {
  const durations = routeDurations({ durations: values })
  return durations.reduce((ranges, duration) => {
    const last = ranges.at(-1)
    if (last && duration === last.end + 1) last.end = duration
    else ranges.push({ start: duration, end: duration })
    return ranges
  }, [])
}

function formatDurationRange(range) {
  return range.start === range.end ? `${range.start}s` : `${range.start}–${range.end}s`
}

function updateDurationSummary(row) {
  const ranges = durationRanges(JSON.parse(row.dataset.durations || '[]'))
  row.querySelector('[data-duration-summary]').textContent = ranges.length ? ranges.map(formatDurationRange).join(', ') : '工作台默认'
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
  menu.innerHTML = `
    <div class="duration-input-row">
      <input data-duration-input type="number" min="1" max="${MAX_DURATION_SECONDS}" step="1" placeholder="秒数" aria-label="输入支持时长">
      <button data-duration-add type="button">添加</button>
    </div>
    <div class="duration-range-row">
      <input data-duration-range-start type="number" min="1" max="${MAX_DURATION_SECONDS}" step="1" placeholder="起始" aria-label="范围起始秒数">
      <span>至</span>
      <input data-duration-range-end type="number" min="1" max="${MAX_DURATION_SECONDS}" step="1" placeholder="结束" aria-label="范围结束秒数">
      <button data-duration-range-add type="button">添加范围</button>
    </div>
    <div data-duration-values class="duration-values"></div>`
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
  const renderValues = () => {
    const durations = routeDurations({ durations: JSON.parse(row.dataset.durations || '[]') })
    row.dataset.durations = JSON.stringify(durations)
    const ranges = durationRanges(durations)
    menu.querySelector('[data-duration-values]').innerHTML = ranges.length
      ? ranges.map((range) => `<span class="duration-value"><span>${formatDurationRange(range)}</span><button data-duration-remove-start="${range.start}" data-duration-remove-end="${range.end}" type="button" aria-label="移除 ${range.start}${range.start === range.end ? '' : ` 至 ${range.end}`} 秒">×</button></span>`).join('')
      : '<span class="duration-empty">未添加，使用工作台默认</span>'
    updateDurationSummary(row)
  }
  const addDuration = () => {
    const input = menu.querySelector('[data-duration-input]')
    const duration = Number(input.value)
    if (!Number.isInteger(duration) || duration < 1 || duration > MAX_DURATION_SECONDS) {
      showToast(`请输入 1-${MAX_DURATION_SECONDS} 之间的整数秒数`, 'error')
      input.focus()
      return
    }
    const durations = routeDurations({ durations: JSON.parse(row.dataset.durations || '[]') })
    row.dataset.durations = JSON.stringify([...durations, duration])
    input.value = ''
    renderValues()
    input.focus()
  }
  const addDurationRange = () => {
    const startInput = menu.querySelector('[data-duration-range-start]')
    const endInput = menu.querySelector('[data-duration-range-end]')
    const start = Number(startInput.value)
    const end = Number(endInput.value)
    if (
      !Number.isInteger(start) || !Number.isInteger(end) ||
      start < 1 || end > MAX_DURATION_SECONDS || start > end
    ) {
      showToast(`请输入有效范围，例如 2 至 15（最大 ${MAX_DURATION_SECONDS} 秒）`, 'error')
      startInput.focus()
      return
    }
    const durations = JSON.parse(row.dataset.durations || '[]')
    const range = Array.from({ length: end - start + 1 }, (_, index) => start + index)
    row.dataset.durations = JSON.stringify([...durations, ...range])
    startInput.value = ''
    endInput.value = ''
    renderValues()
    startInput.focus()
  }
  menu.querySelector('[data-duration-add]').addEventListener('click', addDuration)
  menu.querySelector('[data-duration-range-add]').addEventListener('click', addDurationRange)
  menu.querySelector('[data-duration-input]').addEventListener('keydown', (event) => {
    if (event.key === 'Enter') {
      event.preventDefault()
      addDuration()
    }
  })
  for (const input of menu.querySelectorAll('[data-duration-range-start], [data-duration-range-end]')) {
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter') {
        event.preventDefault()
        addDurationRange()
      }
    })
  }
  menu.addEventListener('click', (event) => {
    const removeButton = event.target.closest('[data-duration-remove-start]')
    if (!removeButton) return
    const start = Number(removeButton.dataset.durationRemoveStart)
    const end = Number(removeButton.dataset.durationRemoveEnd)
    const durations = JSON.parse(row.dataset.durations || '[]').filter((duration) => duration < start || duration > end)
    row.dataset.durations = JSON.stringify(durations)
    renderValues()
  })
  renderValues()
}

function addRouteRow(route = {}) {
  const row = document.createElement('div')
  row.className = 'route-editor-row'
  const mappedUpstreamModel = route.upstream_model || route.mapped_upstream_model || ''
  const selectedDurations = routeDurations(route)
  const protocol = route.protocol || 'videos'
  const selectedProfile = protocol === 'ark-v3' ? (route.profile || 'ark-seedance-2') : (route.profile || 'default')
  row.dataset.durations = JSON.stringify(selectedDurations)
  row.innerHTML = `
    <input data-route-field="model" maxlength="160" value="${escapeHtml(route.model || '')}" placeholder="对外模型名" aria-label="对外模型名">
    <select data-route-field="protocol" aria-label="请求协议">
      <option value="videos"${protocol === 'videos' ? ' selected' : ''}>videos</option>
      <option value="seedance"${protocol === 'seedance' ? ' selected' : ''}>seedance</option>
      <option value="ark-v3"${protocol === 'ark-v3' ? ' selected' : ''}>ark-v3（方舟原生）</option>
    </select>
    <select data-route-field="profile" aria-label="请求格式"${protocol === 'seedance' ? ' disabled' : ''}>${profileOptions(protocol === 'seedance' ? 'default' : selectedProfile)}</select>
    <input data-route-field="upstream_model" maxlength="160" value="${escapeHtml(mappedUpstreamModel)}" placeholder="上游模型名" aria-label="映射上游模型名">
    <button class="duration-picker" data-duration-trigger data-duration-summary type="button">${selectedDurations.length ? durationRanges(selectedDurations).map(formatDurationRange).join(', ') : '工作台默认'}</button>
    <input data-route-field="resolutions" maxlength="620" value="${escapeHtml((route.resolutions || []).join(', '))}" placeholder="工作台默认" aria-label="支持分辨率" title="多个分辨率用逗号分隔">
    <label class="image-count"><input data-route-field="image_count" type="number" min="0" max="30" value="${route.image_count ?? 1}" aria-label="图片数量"><span>张</span></label>
    <label class="media-support-cell"><input data-route-forward="resolution" type="checkbox"${route.forward_resolution !== false ? ' checked' : ''} aria-label="传分辨率"></label>
    <label class="media-support-cell"><input data-route-support="video" type="checkbox"${route.supports_video !== false ? ' checked' : ''} aria-label="支持视频"></label>
    <label class="media-support-cell"><input data-route-support="audio" type="checkbox"${route.supports_audio !== false ? ' checked' : ''} aria-label="支持音频"></label>
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
    const resolutions = [...new Set(row.querySelector('[data-route-field="resolutions"]').value
      .split(/[,，]/).map((value) => value.trim()).filter(Boolean))]
    const imageCount = Math.max(0, Math.min(30, Number(row.querySelector('[data-route-field="image_count"]').value) || 0))
    const effectiveModel = model || upstreamModel
    if (!effectiveModel) throw new Error('每一行都必须填写对外模型名或映射上游模型名')
    return {
      model: preserveBlankModel ? model : effectiveModel,
      upstream_model: upstreamModel,
      protocol: row.querySelector('[data-route-field="protocol"]').value,
      profile: row.querySelector('[data-route-field="profile"]').value,
      durations,
      resolutions,
      duration_override: durations.length === 1 ? durations[0] : null,
      image_count: imageCount,
      supports_image: imageCount > 0,
      forward_resolution: row.querySelector('[data-route-forward="resolution"]').checked,
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
    const discoveredNames = new Set(result.models.map((item) => item.upstream_model))
    const addedRoutes = result.models.filter((item) => !currentByUpstream.has(item.upstream_model))
    const retainedRoutes = currentRoutes.filter((route) => !discoveredNames.has(route.upstream_model || route.model))
    setRouteRows([
      ...result.models.map((item) => currentByUpstream.get(item.upstream_model) || item),
      ...retainedRoutes,
    ])
    const retainedText = retainedRoutes.length ? `，保留 ${retainedRoutes.length} 条未发现的已有路由` : ''
    showToast(`发现 ${result.models.length} 个模型，新增 ${addedRoutes.length} 个${retainedText}`)
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
  const profile = row?.querySelector('[data-route-field="profile"]')
  if (!profile) return
  profile.disabled = target.value === 'seedance'
  if (profile.disabled) {
    profile.value = 'default'
  } else if (target.value === 'ark-v3') {
    profile.value = 'ark-seedance-2'
  } else if (profile.value === 'ark-seedance-2') {
    profile.value = 'default'
  }
})
document.addEventListener('click', (event) => {
  if (!activeDurationMenu) return
  const target = event.target instanceof Node ? event.target : null
  if (activeDurationMenu.menu.contains(target) || (target instanceof Element && target.matches('[data-duration-trigger]'))) return
  closeDurationMenu()
})
window.addEventListener('resize', closeDurationMenu)

document.querySelector('#add-upstream').addEventListener('click', () => openDialog())
integrationDocumentButton.addEventListener('click', downloadIntegrationDocument)
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
publicVideoSettingsForm.addEventListener('submit', async (event) => {
  event.preventDefault()
  const button = publicVideoSettingsForm.querySelector('button[type="submit"]')
  button.disabled = true
  try {
    const result = await api('/admin/api/settings/public-video', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ download_limit: Number(publicVideoDownloadLimit.value) }),
    })
    dashboard.public_video_download_limit = result.public_video_download_limit
    publicVideoDownloadLimit.value = result.public_video_download_limit
    showToast('下载上限已保存')
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
dialog.addEventListener('cancel', (event) => event.preventDefault())
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
