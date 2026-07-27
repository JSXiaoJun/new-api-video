const form = document.querySelector('#login-form')
const errorBox = document.querySelector('#login-error')

form.addEventListener('submit', async (event) => {
  event.preventDefault()
  errorBox.hidden = true
  const button = form.querySelector('button[type="submit"]')
  button.disabled = true
  try {
    const data = new FormData(form)
    const response = await fetch('/admin/api/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: data.get('username'), password: data.get('password') }),
    })
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}))
      throw new Error(payload.detail || '登录失败')
    }
    window.location.assign('/admin')
  } catch (error) {
    errorBox.textContent = error.message
    errorBox.hidden = false
  } finally {
    button.disabled = false
  }
})

