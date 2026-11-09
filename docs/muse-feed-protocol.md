# Muse 动态只读协议记录

核对日期：2026-11-09。来源：已登录 `https://muse.ai/feed` 加载的 JavaScript，通过 js-reverse 读取；不是对公网 REST 接口的猜测。未调用动态生成、点赞、删除等写接口。

## 路由

网页构建 `dpl_HTm1U6ezMksdqdRU3B8HCG8rf2Qx`：

| RPC 名称 | 加密隧道内 HTTP 请求 | 用途 |
| --- | --- | --- |
| `feed.list` | `GET /api/feed` | 已有动态列表 |
| `feed.unit.media` | `GET /api/feed/units/{unit_id}/media/{handle}` | 受保护的图片/媒体字节 |

- 路由表：`https://muse.ai/_next/static/chunks/3oyykwd4lzys6.js`。
- 隧道传输：`1gheaqu8ekd80.js`；未指定 service 时使用 `daemon`，上述 feed 路由未覆盖该值。
- 两者均复用现有 Muse Noise 认证连接。不能直接理解为对 `https://muse.ai/api/feed` 的普通 HTTP GET。
- `feed.infinite` 是另一个流式、按需内容机制，不作为历史翻页接口使用，本实现不调用。

## 参数和原始返回值

`24-83jdwkty3y.js` 模块 `942970`（约偏移 47800–55000）包含解析与请求构造：

- 首次读取：`timezone`，可选 `prompt_id`。
- 翻页：`cursor = pagination.next_cursor`，保留返回的 `prompt_id` 及 timezone。
- 未发现网页发送 `limit` 或 `page_size`；本实现本地截断并限制最多十页，不假设服务端页大小。

```text
{
  prompt_id,
  days: [{
    local_date,
    editions: [{
      kind, title, generated_at_ms,
      units: [{
        unit_id, run_id, edition_local_date,
        edition_kind, edition_generated_at_ms, position,
        kicker, title, body_md,
        created_at_ms, updated_at_ms,
        attachment, ...
      }]
    }]
  }],
  pagination: {next_cursor, prev_cursor}
}
```

实现取 `unit_id` 为去重 ID、`created_at_ms / 1000` 为创建时间，拼接 title / body_md。未知或零时间不自动发表；不以扫描时间代替创建时间。只读取现有动态，不生成新动态。

## 图片

- `hero_image`：`hero_image_url`。
- `carousel`：`image_urls`，有对应 `media_items` 时视频使用 `poster_url`，不把视频当图片上传。
- `social_embed`：`social_media_items` 中 kind=image 的 `media_url`，没有图片时使用 `social_thumbnail_url`。
- `widget`：不执行、不转发 `widget_html`。

`15u214svr5vbk.js`（约偏移 34000–37000）中 `tw` / `tj` 表明：路径含 `/feed/units/{unitId}/media/{handle}` 的媒体需要经 `feed.unit.media` 读取并保留查询参数；不是保证公开可访问的图片地址。本实现仅在选中待发送动态后获取这些图片，再使用 `base64://` 交给 OneBot。普通 HTTP(S) 图片仍由 OneBot 后端上传。

本实现限制受保护图片的大小、检查常见图片文件签名，并拒绝跨动态媒体路径和路径穿越。图片准备失败时不退化为无图发表。没有实现视频空间发表。

## 传输信封（真实抓取修正）

2026-11-09 实际请求 `/api/feed?timezone=Asia/Shanghai`（`service=daemon`）得到 22437 字节，真实外壳比网页解析函数多一层：

```json
{"ok": true, "result": {"prompt_id": "...", "days": [...], "pagination": {...}}}
```

网页端的 `sendRequest` 已拆掉这层，所以源码里只看到 `result` 内部结构。Python 侧必须自行拆封：`ok` 不为 `true` 视为失败，不再继续解析。`parse_feed` 与 `fetch_feed` 都经过 `_unwrap`，也接受未包装的裸结构（便于测试与版本差异）。

## 验证边界

- 已确认（真实调用）：凭据可刷新；`GET /api/feed` 返回 10 条动态；标题/正文/时间戳/图片计数解析正确；受保护图片经媒体接口取回并以 `base64://` 交给后端；`send_qzone_msg` 发表成功且 `get_qzone_msg_list` 可回读同一 `tid`；状态文件记录 `unit_id`，同一动态不会重复发表。
- 未确认：分页 `cursor` 的真实翻页行为（本次首页已满足 `scan_limit`，没有触发第二页）；视频发布（不支持，只取封面）；空间可见范围 `1`、`64` 之外的行为；服务端页大小。
- 测试期间实际发表过 2 条动态，均为「仅自己可见」，其中 1 条为单条转发测试。
