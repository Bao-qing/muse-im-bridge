// 监听所有请求
addEventListener('fetch', event => {
  event.respondWith(handleRequest(event.request))
})

async function handleRequest(request) {
  // 解析请求的 URL
  const url = new URL(request.url)
  const path = url.pathname   // 例如 /https/api.demo.com/path

  // 使用正则提取协议（http、https、ws 或 wss）和后续路径
  // 格式：/协议/域名+路径
  const match = path.match(/^\/(https?|wss?)\/(.*)$/)
  if (!match) {
    return new Response('Invalid path format. Expected: /{protocol}/{host}/{path}', {
      status: 400
    })
  }

  const protocol = match[1]          // 如 "https"、"http"、"wss" 或 "ws"
  let rest = match[2]               // 如 "api.demo.com/path"

  // 处理用户可能输入的多余斜杠（如 /https//api.demo.com）
  rest = rest.replace(/^\/+/, '')

  // WebSocket 请求走中继逻辑
  if (protocol === 'ws' || protocol === 'wss') {
    return handleWebSocket(request, protocol, rest, url)
  }

  // 构建目标 URL：协议 + 域名路径 + 原始查询参数
  const targetUrl = `${protocol}://${rest}${url.search}`

  // 创建新请求，复用原请求的所有属性（方法、headers、body 等）
  // Cloudflare 会自动将 Host header 设置为目标域名
  const newRequest = new Request(targetUrl, request)

  try {
    // 发起转发请求并返回响应
    const response = await fetch(newRequest)
    return response
  } catch (error) {
    return new Response('Proxy error: ' + error.message, {
      status: 502
    })
  }
}

async function handleWebSocket(request, protocol, rest, url) {
  if ((request.headers.get('Upgrade') || '').toLowerCase() !== 'websocket') {
    return new Response('WebSocket upgrade required', {
      status: 400
    })
  }

  // fetch 只认 http/https：ws 对应 http，wss 对应 https
  const targetUrl = `${protocol === 'wss' ? 'https' : 'http'}://${rest}${url.search}`

  // 准备客户端侧的 WebSocket
  const pair = new WebSocketPair()
  const client = pair[0]
  const server = pair[1]
  server.accept()

  // 透传原始请求头（去掉由 Cloudflare 自行生成的几个），向目标发起握手
  const headers = new Headers(request.headers)
  headers.delete('host')
  headers.delete('connection')
  headers.delete('sec-websocket-key')
  headers.delete('sec-websocket-version')
  headers.set('Upgrade', 'websocket')

  let upstreamResp
  try {
    upstreamResp = await fetch(targetUrl, { headers })
  } catch (error) {
    try { server.close(1011, 'upstream connect failed') } catch (e) {}
    return new Response('Proxy error: ' + error.message, {
      status: 502
    })
  }
  const upstream = upstreamResp.webSocket
  if (!upstream) {
    try { server.close(1011, 'upstream refused websocket') } catch (e) {}
    return new Response('Upstream did not accept WebSocket', {
      status: 502
    })
  }
  upstream.accept()

  // 双向转发消息（文本与二进制均原样透传）
  server.addEventListener('message', event => {
    try { upstream.send(event.data) } catch (e) {}
  })
  upstream.addEventListener('message', event => {
    try { server.send(event.data) } catch (e) {}
  })
  server.addEventListener('close', event => {
    try { upstream.close(event.code, event.reason) } catch (e) {}
  })
  upstream.addEventListener('close', event => {
    try { server.close(event.code, event.reason) } catch (e) {}
  })
  server.addEventListener('error', () => {
    try { upstream.close(1011) } catch (e) {}
  })
  upstream.addEventListener('error', () => {
    try { server.close(1011) } catch (e) {}
  })

  const responseHeaders = {}
  const chosen = upstreamResp.headers.get('Sec-WebSocket-Protocol')
  if (chosen) responseHeaders['Sec-WebSocket-Protocol'] = chosen
  return new Response(null, {
    status: 101,
    webSocket: client,
    headers: responseHeaders
  })
}
