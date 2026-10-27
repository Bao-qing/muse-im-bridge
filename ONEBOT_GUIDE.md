# OneBot (NapCat) 接口对接与使用指南

本文档整理了本项目所使用的 OneBot 服务端（NapCat.Onebot v11）的连接配置、消息推送、消息获取、代码示例以及 Windows 终端编码避坑说明。

---

## 1. 服务端基础信息

- **服务端实现**: NapCat.Onebot (v4.18.0)
- **协议版本**: OneBot v11
- **HTTP 基地址 (Base URL)**: `https://bot.baoqing.eu.org:81`
- **鉴权方式**: HTTP Header 携带 Bearer Token
  ```http
  Authorization: Bearer baoqing666
  ```
- **数据交互格式**: `application/json; charset=utf-8`
- **消息推荐格式**: **`Array`**（消息段数组格式，富文本友好，抗转义）

---

## 2. 消息推送接口 (发送消息)

### 2.1 发送群消息 (`/send_group_msg`)

- **方法**: `POST`
- **路径**: `/send_group_msg`
- **请求体（JSON）**:
  ```json
  {
    "group_id": 836401578,
    "message": [
      {
        "type": "text",
        "data": {
          "text": "这里填写要发送的消息内容"
        }
      }
    ]
  }
  ```

### 2.2 发送私聊消息 (`/send_private_msg`)

- **方法**: `POST`
- **路径**: `/send_private_msg`
- **请求体（JSON）**:
  ```json
  {
    "user_id": 123456789,
    "message": [
      {
        "type": "text",
        "data": {
          "text": "私聊通知内容"
        }
      }
    ]
  }
  ```

### 2.3 消息格式说明（Array vs String）
在配置消息推送的「消息格式」下拉框中，请务必选择 **`Array`**：
- **`Array`（推荐）**：符合 OneBot 规范的消息段数组，支持混合发送文本、图片、@群成员等富文本，且不会将消息中的 `[CQ:...]` 误当成纯文本转义；
- **`String`**：纯字符串或拼接 CQ 码（如 `[CQ:at,qq=123456] 内容`），当正文含有特殊符号时易解析异常。

---

## 3. 消息获取接口 (接收与拉取)

### 3.1 获取群历史消息 (`/get_group_msg_history`)

- **方法**: `POST`
- **路径**: `/get_group_msg_history`
- **请求体（JSON）**:
  ```json
  {
    "group_id": 836401578,
    "count": 20
  }
  ```
- **关键响应字段**:
  ```json
  {
    "status": "ok",
    "retcode": 0,
    "data": {
      "messages": [
        {
          "message_id": 1760768394,
          "message_seq": 1760768394,
          "message_type": "group",
          "group_id": 836401578,
          "time": 1793098174,
          "sender": {
            "user_id": 3768373591,
            "nickname": "August",
            "role": "owner"
          },
          "raw_message": "消息文本内容",
          "message": [
            {
              "type": "text",
              "data": {
                "text": "消息文本内容"
              }
            }
          ]
        }
      ]
    }
  }
  ```

### 3.2 根据消息 ID 查询单条消息 (`/get_msg`)

- **方法**: `POST`
- **路径**: `/get_msg`
- **请求体（JSON）**:
  ```json
  {
    "message_id": 1760768394
  }
  ```

### 3.3 实时消息监听 (WebSocket)

如果需要实时监听群消息（而不是定时调用 HTTP 轮询），可以使用 WebSocket：
- **正向 WebSocket**: 客户端主动连接 `wss://bot.baoqing.eu.org:81/?access_token=baoqing666`
- **事件流格式**: 群内成员发言时，服务端会即时推送 JSON 消息包，通过匹配 `"post_type": "message"` 和 `"message_type": "group"` 即可获取实时发言。

---

## 4. Python 完整集成示例

```python
import requests
import urllib3

# 如使用自签名证书或临时域名，可屏蔽安全警告
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

ONEBOT_BASE = "https://bot.baoqing.eu.org:81"
ONEBOT_TOKEN = "baoqing666"

HEADERS = {
    "Authorization": f"Bearer {ONEBOT_TOKEN}",
    "Content-Type": "application/json; charset=utf-8",
}

def send_group_text(group_id: int, content: str):
    """向指定群发送消息（Array格式）"""
    url = f"{ONEBOT_BASE}/send_group_msg"
    payload = {
        "group_id": group_id,
        "message": [
            {
                "type": "text",
                "data": {"text": content}
            }
        ]
    }
    resp = requests.post(url, headers=HEADERS, json=payload, verify=False, timeout=10)
    data = resp.json()
    if data.get("retcode") == 0:
        print(f"发送成功，消息ID: {data['data']['message_id']}")
    else:
        print(f"发送失败: {data}")
    return data

def get_group_messages(group_id: int, count: int = 10):
    """获取指定群的最近历史消息"""
    url = f"{ONEBOT_BASE}/get_group_msg_history"
    payload = {
        "group_id": group_id,
        "count": count
    }
    resp = requests.post(url, headers=HEADERS, json=payload, verify=False, timeout=10)
    data = resp.json()
    if data.get("retcode") == 0:
        messages = data.get("data", {}).get("messages", [])
        for m in messages:
            sender = m.get("sender", {}).get("nickname", "未知")
            text = m.get("raw_message", "")
            print(f"[{sender}]: {text}")
        return messages
    else:
        print(f"获取失败: {data}")
        return []

if __name__ == "__main__":
    test_group = 836401578
    # 1. 测试拉取历史记录
    print("--- 读取群消息 ---")
    get_group_messages(test_group, count=3)
    
    # 2. 测试发送消息
    print("\n--- 发送测试消息 ---")
    send_group_text(test_group, "来自 Python 的测试推送！")
```

---

## 5. Windows 终端中文乱码排查与防范

在 Windows 上测试 OneBot 接口时，若遇到中文乱码（显示为 `` 菱形问号），通常为以下原因：

1. **终端代码页与 UTF-8 冲突**：
   - 简体中文 Windows 默认使用 GBK（代码页 936）。
   - PowerShell 可通过在 profile 中添加 `chcp 65001 > $null` 以及设置 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8` 解决。
2. **Python 环境变量**：
   - 设置用户环境变量 `PYTHONUTF8=1` 与 `PYTHONIOENCODING=utf-8`。
3. **cURL 命令行传参损耗**：
   - 在 Git Bash 或 CMD 中直接通过命令行 `-d '{"text": "中文"}'` 传递字符时，Windows ANSI 层可能会破坏字节流；
   - 推荐将数据保存到 JSON 文件后使用 `curl -d @data.json` 发送，或者在请求中使用 Unicode 转义（如 `一条`）。
