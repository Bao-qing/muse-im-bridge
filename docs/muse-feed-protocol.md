# Muse 动态只读协议

本文说明 Muse 动态（Feed）的只读拉取、响应信封与媒体处理协议。桥接实现仅读取已有动态，不调用动态生成、点赞、删除等写接口。

## 路由

| RPC 名称 | 加密隧道内 HTTP 请求 | 用途 |
| --- | --- | --- |
| `feed.list` | `GET /api/feed` | 已有动态列表 |
| `feed.unit.media` | `GET /api/feed/units/{unit_id}/media/{handle}` | 受保护的图片/媒体字节 |

- 两个接口均经现有 Muse Noise 认证隧道路由至 `daemon` 服务，并非直接对公网 `https://muse.ai/api/feed` 发起普通 HTTP GET。
- `feed.infinite` 属于流式按需内容机制，不作为历史翻页接口，本实现不调用。

## 请求参数与响应信封

- 首次读取：携带 `timezone`，可选 `prompt_id`。
- 翻页：携带 `cursor = pagination.next_cursor`，并保留返回的 `prompt_id` 及 `timezone`。
- 服务端接口未约定 `limit` / `page_size` 参数；桥接实现本地截断并限制最多翻 10 页，不假设服务端页大小。

隧道内返回的顶层 JSON 信封格式如下：

```json
{
  "ok": true,
  "result": {
    "prompt_id": "...",
    "days": [
      {
        "local_date": "...",
        "editions": [
          {
            "kind": "...",
            "title": "...",
            "generated_at_ms": 0,
            "units": [
              {
                "unit_id": "...",
                "run_id": "...",
                "edition_local_date": "...",
                "edition_kind": "...",
                "edition_generated_at_ms": 0,
                "position": 0,
                "kicker": "...",
                "title": "...",
                "body_md": "...",
                "created_at_ms": 0,
                "updated_at_ms": 0,
                "attachment": {}
              }
            ]
          }
        ]
      }
    ],
    "pagination": {
      "next_cursor": "...",
      "prev_cursor": "..."
    }
  }
}
```

- `ok` 不为 `true` 时视为请求失败，中止解析；解析层通过 `_unwrap` 拆封外层信封，同时兼容无外层信封的裸结构。
- 实现以 `unit_id` 作为去重键、以 `created_at_ms / 1000` 作为创建时间，并拼接 `title` 与 `body_md`。未知或零时间戳的条目不自动发表，不以扫描时间代替创建时间。

## 图片与媒体处理

- `hero_image`：取 `hero_image_url`。
- `carousel`：取 `image_urls`；存在对应 `media_items` 且为视频时仅取 `poster_url` 封面，不把视频当图片上传。
- `social_embed`：优先取 `social_media_items` 中 `kind=image` 的 `media_url`，否则回退到 `social_thumbnail_url`。
- `widget`：不执行、不转发 `widget_html`。

路径含 `/feed/units/{unitId}/media/{handle}` 的媒体属于受保护资源，必须经 `feed.unit.media`（保留原查询参数）拉取字节，仅在选中待发送动态后获取，并以 `base64://` 交给 OneBot 或按目标平台能力上传；普通 HTTP(S) 图片按平台默认方式处理。

实现限制受保护图片的大小、校验常见图片文件签名，并拒绝跨动态媒体路径和路径穿越。图片准备失败时不退化为无图发表；当前不支持视频动态发表。
