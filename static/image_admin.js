const csrf = document.querySelector('meta[name="csrf-token"]').content
const dialog = document.querySelector('#image-upstream-dialog')
const form = document.querySelector('#image-upstream-form')
const toast = document.querySelector('#toast')
const routeRows = document.querySelector('#image-route-rows')
const modelSelectionDialog = document.querySelector('#image-model-selection-dialog')
const modelSelectionSearch = document.querySelector('#image-model-selection-search')
const modelSelectionList = document.querySelector('#image-model-selection-list')
const modelSelectionSelectAll = document.querySelector('#image-model-selection-select-all')
const modelSelectionConfirm = document.querySelector('#image-model-selection-confirm')
const logSummary = document.querySelector('#image-log-summary')
const logPagination = document.querySelector('#image-log-pagination')
const logPageTotal = document.querySelector('#image-log-page-total')
const logPageIndicator = document.querySelector('#image-log-page-indicator')
const logPagePrev = document.querySelector('#image-log-page-prev')
const logPageNext = document.querySelector('#image-log-page-next')
const logPageSizeSelect = document.querySelector('#image-log-page-size')
let dashboard = { upstreams: [], requests: [], stats: {} }
let pendingSyncEntries = []
let pendingRouteSnapshot = []
let selectedAddedModels = new Set()
let selectedRemovedIndexes = new Set()
let logPage = 1

function updateResponsiveClass() {
  document.documentElement.classList.toggle('is-mobile', window.matchMedia('(max-width: 760px)').matches)
}

updateResponsiveClass()
window.addEventListener('resize', updateResponsiveClass)

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>'"]/g, (char) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', "'": '&#39;', '"': '&quot;' })[char])
}

const toastIsPopover = typeof toast.showPopover === 'function'
if (toastIsPopover) toast.removeAttribute('hidden')

function showToast(message, tone = 'default') {
  toast.textContent = message
  toast.dataset.tone = tone
  if (toastIsPopover) {
    if (!toast.matches(':popover-open')) toast.showPopover()
  } else {
    toast.hidden = false
  }
  clearTimeout(showToast.timer)
  showToast.timer = setTimeout(() => {
    if (!toastIsPopover) {
      toast.hidden = true
      return
    }
    if (toast.matches(':popover-open')) toast.hidePopover()
  }, 2800)
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

function apiFormatLabel(apiFormat) {
  return apiFormat === 'gemini' ? 'Gemini 原生' : 'OpenAI Images'
}

function resultLabel(item) {
  if (item.success) return '成功'
  if (item.error === 'No usable image data returned') return '无图片结果'
  return item.http_status == null ? '连接失败' : `HTTP ${item.http_status}`
}

function routeBadges(route) {
  return `
    <span>${escapeHtml(route.public_model)} → ${escapeHtml(route.upstream_model)}<small>${formatCost(route.cost_per_request)}</small></span>`
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
        <td><code>${escapeHtml(upstream.base_url)}</code><small class="health-detail">${escapeHtml(apiFormatLabel(upstream.api_format))}</small></td>
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
  renderLogPagination()
}

function renderLogPagination() {
  const page = dashboard.pagination
  const total = page?.total ?? dashboard.requests.length
  logSummary.textContent = total ? `共 ${total} 条记录` : '暂无记录'
  if (!total) {
    logPagination.hidden = true
    return
  }
  logPagination.hidden = false
  const first = (page.page - 1) * page.page_size + 1
  logPageTotal.textContent = `第 ${first} – ${first + dashboard.requests.length - 1} 条`
  logPageIndicator.textContent = `${page.page} / ${page.pages}`
  logPagePrev.disabled = page.page <= 1
  logPageNext.disabled = page.page >= page.pages
}

function formatMegabytes(value) {
  const megabytes = Number(value || 0)
  if (megabytes >= 1024) return `${(megabytes / 1024).toFixed(2)} GB`
  return `${megabytes.toFixed(1)} MB`
}

async function loadStorage() {
  const report = await api('/admin/api/images/storage')
  document.querySelector('#storage-files').textContent = report.files
  document.querySelector('#storage-used').textContent = formatMegabytes(report.tracked_megabytes)
  document.querySelector('#storage-cap').textContent = formatMegabytes(report.max_megabytes)
  document.querySelector('#storage-free').textContent = formatMegabytes(report.free_disk_bytes / 1024 / 1024)
  document.querySelector('#image-storage-detail').textContent =
    `图片 ${report.asset_retention_seconds / 3600} 小时过期 · 日志保留 ${report.log_retention_seconds / 3600} 小时 · 上限 ${formatMegabytes(report.max_megabytes)}（超出后先删除最早的图片）`
}

async function loadDashboard() {
  dashboard = await api('/admin/api/images/dashboard')
  logPage = dashboard.pagination?.page || 1
  render()
}

async function loadLogs() {
  const params = new URLSearchParams()
  const query = document.querySelector('#image-log-search').value.trim()
  const outcome = document.querySelector('#image-log-outcome').value
  if (query) params.set('q', query)
  if (outcome) params.set('outcome', outcome)
  params.set('page', logPage)
  params.set('page_size', logPageSizeSelect.value)
  const result = await api(`/admin/api/images/requests?${params}`)
  dashboard.requests = result.requests
  dashboard.pagination = result.pagination
  logPage = result.pagination.page
  render()
}

async function goToLogPage(page) {
  logPage = page
  try {
    await loadLogs()
  } catch (error) {
    showToast(error.message, 'error')
  }
}

function addRouteRow(route = {}) {
  const row = document.createElement('div')
  row.className = 'route-grid image-route-row'
  row.innerHTML = `
    <input data-route-field="public_model" required maxlength="160" value="${escapeHtml(route.public_model || '')}" placeholder="gpt-image-2" aria-label="公开模型">
    <input data-route-field="upstream_model" required maxlength="160" list="image-model-suggestions" value="${escapeHtml(route.upstream_model || '')}" placeholder="gpt-image-2-pro" aria-label="上游模型">
    <input data-route-field="cost" type="number" min="0" max="100000" step="0.000001" required value="${Number(route.cost_per_request || 0)}" aria-label="每次成本">
    <button class="route-remove" type="button" aria-label="删除路由">×</button>`
  routeRows.appendChild(row)
}

function closeDialog() {
  dialog.close()
  form.reset()
  routeRows.innerHTML = ''
}

function routeUpstreamName(route) {
  return route.upstream_model || route.public_model
}

function matchesSyncQuery(entry, query) {
  return !query || entry.model.toLowerCase().includes(query)
}

function visibleSyncCheckboxes() {
  return [...modelSelectionList.querySelectorAll('.model-selection-item:not(.sync-hidden) [data-image-sync-select]')]
}

function updateImageModelSelectionState() {
  const checkboxes = visibleSyncCheckboxes()
  const addedCount = selectedAddedModels.size
  const removedCount = selectedRemovedIndexes.size
  const selectedCount = addedCount + removedCount
  modelSelectionConfirm.disabled = selectedCount === 0
  modelSelectionConfirm.textContent = addedCount || removedCount
    ? `同步选中项（新增 ${addedCount} · 移除 ${removedCount}）`
    : '同步选中项'
  const visibleSelected = checkboxes.filter((checkbox) => checkbox.checked).length
  modelSelectionSelectAll.checked = checkboxes.length > 0 && visibleSelected === checkboxes.length
  modelSelectionSelectAll.indeterminate = visibleSelected > 0 && visibleSelected < checkboxes.length
}

function syncEntryMarkup(entry, hidden) {
  const rowClass = `model-selection-item${hidden ? ' sync-hidden' : ''}`
  if (entry.kind === 'added') {
    return `
      <label class="${rowClass}">
        <input type="checkbox" data-image-sync-select data-image-sync-add="${escapeHtml(entry.model)}"${selectedAddedModels.has(entry.model) ? ' checked' : ''}>
        <span><strong>${escapeHtml(entry.model)}</strong><small>公开模型与上游模型同名，可稍后修改</small></span>
      </label>`
  }
  const detail = entry.route.public_model === entry.route.upstream_model
    ? '上游已不返回该模型'
    : `当前别名 ${escapeHtml(entry.route.public_model)} · 上游已不返回`
  return `
      <label class="${rowClass}">
        <input type="checkbox" data-image-sync-select data-image-sync-remove="${entry.index}"${selectedRemovedIndexes.has(entry.index) ? ' checked' : ''}>
        <span><strong>${escapeHtml(entry.model)}</strong><small class="sync-removal">${detail}</small></span>
      </label>`
}

function renderImageModelSelection() {
  const query = modelSelectionSearch.value.trim().toLowerCase()
  const isHidden = (entry) => !matchesSyncQuery(entry, query)
  if (pendingSyncEntries.every(isHidden)) {
    modelSelectionList.innerHTML = '<p class="model-selection-empty">没有匹配的模型</p>'
    updateImageModelSelectionState()
    return
  }
  const added = pendingSyncEntries.filter((entry) => entry.kind === 'added')
  const removed = pendingSyncEntries.filter((entry) => entry.kind === 'removed')
  const groups = []
  if (added.length) {
    const visibleAdded = added.some((entry) => !isHidden(entry))
    groups.push(`<p class="model-selection-group"${visibleAdded ? '' : ' hidden'}>尚未加入路由 · 勾选后新增</p>`)
    groups.push(added.map((entry) => syncEntryMarkup(entry, isHidden(entry))).join(''))
  }
  if (removed.length) {
    const visibleRemoved = removed.some((entry) => !isHidden(entry))
    groups.push(`<p class="model-selection-group"${visibleRemoved ? '' : ' hidden'}>上游已不返回 · 默认移除，取消勾选可保留</p>`)
    groups.push(removed.map((entry) => syncEntryMarkup(entry, isHidden(entry))).join(''))
  }
  modelSelectionList.innerHTML = groups.join('')
  modelSelectionList.querySelectorAll('[data-image-sync-select]').forEach((checkbox) => {
    checkbox.addEventListener('change', () => {
      if (checkbox.dataset.imageSyncAdd) {
        if (checkbox.checked) selectedAddedModels.add(checkbox.dataset.imageSyncAdd)
        else selectedAddedModels.delete(checkbox.dataset.imageSyncAdd)
      } else if (checkbox.checked) {
        selectedRemovedIndexes.add(Number(checkbox.dataset.imageSyncRemove))
      } else {
        selectedRemovedIndexes.delete(Number(checkbox.dataset.imageSyncRemove))
      }
      updateImageModelSelectionState()
    })
  })
  updateImageModelSelectionState()
}

function openImageModelSelection({ additions, removals, snapshot }) {
  pendingSyncEntries = [
    ...additions.map((model) => ({ kind: 'added', model, index: null, route: null })),
    ...removals.map(({ route, index }) => ({ kind: 'removed', model: routeUpstreamName(route), index, route })),
  ]
  pendingRouteSnapshot = snapshot
  selectedAddedModels = new Set()
  selectedRemovedIndexes = new Set(removals.map(({ index }) => index))
  modelSelectionSearch.value = ''
  modelSelectionSelectAll.checked = false
  modelSelectionSelectAll.indeterminate = false
  renderImageModelSelection()
  modelSelectionDialog.showModal()
  modelSelectionSearch.focus()
}

function closeImageModelSelection() {
  pendingSyncEntries = []
  pendingRouteSnapshot = []
  selectedAddedModels = new Set()
  selectedRemovedIndexes = new Set()
  modelSelectionDialog.close()
}

function renderImageRoutes(routes) {
  routeRows.innerHTML = ''
  for (const route of routes) addRouteRow(route)
  if (!routeRows.children.length) addRouteRow()
}

function openDialog(upstream = null) {
  document.querySelector('#image-dialog-title').textContent = upstream ? '编辑画图上游' : '添加画图上游'
  document.querySelector('#image-upstream-id').value = upstream?.id || ''
  document.querySelector('#image-upstream-name').value = upstream?.name || ''
  document.querySelector('#image-upstream-priority').value = upstream?.priority ?? 100
  document.querySelector('#image-upstream-base-url').value = upstream?.base_url || ''
  document.querySelector('#image-upstream-api-key').value = ''
  document.querySelector('#image-upstream-api-format').value = upstream?.api_format || 'openai'
  document.querySelector('#image-upstream-enabled').checked = upstream?.enabled ?? true
  document.querySelector('#delete-image-upstream').hidden = !upstream
  document.querySelector('#image-form-error').hidden = true
  routeRows.innerHTML = ''
  for (const route of upstream?.routes || [{}]) addRouteRow(route)
  dialog.showModal()
  document.querySelector('#image-upstream-name').focus()
}

function collectRoutes() {
  return [...document.querySelectorAll('.image-route-row')].map((row) => ({
    public_model: row.querySelector('[data-route-field="public_model"]').value.trim(),
    upstream_model: row.querySelector('[data-route-field="upstream_model"]').value.trim(),
    cost_per_request: Number(row.querySelector('[data-route-field="cost"]').value),
  }))
}

document.querySelector('#add-image-upstream').addEventListener('click', () => openDialog())
document.querySelector('#add-image-route').addEventListener('click', () => addRouteRow())
document.querySelector('#close-image-dialog').addEventListener('click', closeDialog)
document.querySelector('#cancel-image-dialog').addEventListener('click', closeDialog)
document.querySelector('#close-image-model-selection').addEventListener('click', closeImageModelSelection)
document.querySelector('#cancel-image-model-selection').addEventListener('click', closeImageModelSelection)
modelSelectionSearch.addEventListener('input', renderImageModelSelection)
modelSelectionSelectAll.addEventListener('change', () => {
  visibleSyncCheckboxes().forEach((checkbox) => {
    checkbox.checked = modelSelectionSelectAll.checked
    const addModel = checkbox.dataset.imageSyncAdd
    const removeIndex = Number(checkbox.dataset.imageSyncRemove)
    if (addModel) {
      if (checkbox.checked) selectedAddedModels.add(addModel)
      else selectedAddedModels.delete(addModel)
    } else if (checkbox.checked) {
      selectedRemovedIndexes.add(removeIndex)
    } else {
      selectedRemovedIndexes.delete(removeIndex)
    }
  })
  updateImageModelSelectionState()
})
modelSelectionConfirm.addEventListener('click', () => {
  const additions = pendingSyncEntries
    .filter((entry) => entry.kind === 'added' && selectedAddedModels.has(entry.model))
    .map((entry) => ({ public_model: entry.model, upstream_model: entry.model, cost_per_request: 0 }))
  const kept = pendingRouteSnapshot.filter((_, index) => !selectedRemovedIndexes.has(index))
  const removedCount = selectedRemovedIndexes.size
  closeImageModelSelection()
  renderImageRoutes([...kept, ...additions])
  showToast(`已同步路由：新增 ${additions.length} 条、移除 ${removedCount} 条`)
})
modelSelectionDialog.addEventListener('click', (event) => {
  if (event.target === modelSelectionDialog) closeImageModelSelection()
})
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

document.querySelector('#image-storage-cleanup').addEventListener('click', async (event) => {
  const button = event.currentTarget
  button.disabled = true
  try {
    const result = await api('/admin/api/images/storage/cleanup', { method: 'POST' })
    const cleaned = result.cleaned
    await loadStorage()
    showToast(`已清理：过期图片 ${cleaned.expired_files} 个、容量淘汰 ${cleaned.evicted_files} 个、日志 ${cleaned.request_logs} 条`)
  } catch (error) {
    showToast(error.message, 'error')
  } finally {
    button.disabled = false
  }
})

document.querySelector('#refresh-image-button').addEventListener('click', async () => {
  try {
    await loadDashboard()
    await loadStorage()
    showToast('数据已刷新')
  } catch (error) {
    showToast(error.message, 'error')
  }
})

document.querySelector('#image-log-filter').addEventListener('submit', async (event) => {
  event.preventDefault()
  await goToLogPage(1)
})
logPagePrev.addEventListener('click', () => goToLogPage(logPage - 1))
logPageNext.addEventListener('click', () => goToLogPage(logPage + 1))
logPageSizeSelect.addEventListener('change', () => goToLogPage(1))

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
    const snapshot = collectRoutes().filter((route) => route.public_model || route.upstream_model)
    const routed = new Set(snapshot.map(routeUpstreamName))
    const discovered = new Set(result.models)
    const additions = result.models.filter((model) => !routed.has(model))
    const removals = snapshot
      .map((route, index) => ({ route, index }))
      .filter(({ route }) => !discovered.has(routeUpstreamName(route)))
    if (!additions.length && !removals.length) {
      showToast(`已获取 ${result.models.length} 个上游模型，路由已是最新`)
    } else {
      openImageModelSelection({ additions, removals, snapshot })
      showToast(`上游返回 ${result.models.length} 个模型：新增 ${additions.length} 个、已不返回 ${removals.length} 个`)
    }
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
      throw new Error('请完整填写至少一条模型路由')
    }
    await api(id ? `/admin/api/images/upstreams/${id}` : '/admin/api/images/upstreams', {
      method: id ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: document.querySelector('#image-upstream-name').value,
        priority: Number(document.querySelector('#image-upstream-priority').value),
        base_url: document.querySelector('#image-upstream-base-url').value,
        api_key: document.querySelector('#image-upstream-api-key').value,
        api_format: document.querySelector('#image-upstream-api-format').value,
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
loadStorage().catch((error) => showToast(error.message, 'error'))
