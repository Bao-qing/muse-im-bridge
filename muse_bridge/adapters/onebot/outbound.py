"""OneBot platform-only delivery of text, native quotes and media."""
from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack

from ...core.delivery import DeliveryEnvelope, DeliveryReceipt, DeliveryResult, DeliveryStatus
from ...core.message import chunk_text
from .client import OneBotAPIError, media_segment, text_segment

GROUP_FALLBACK_PREFIX = "【群消息提醒】\n"


class OneBotSender:
    def __init__(self, config, client):
        self.config, self.client = config, client
        self._locks = {}
        self._uncertain = {}

    async def deliver(self, envelope: DeliveryEnvelope) -> DeliveryResult:
        if not envelope.delivery_id or envelope.target_type not in {"private", "group"} or not envelope.target_id:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="Invalid OneBot delivery ID or target")
        key = (envelope.delivery_id, envelope.target_type, envelope.target_id)
        targets = {(envelope.target_type, envelope.target_id)}
        if envelope.target_type == "group" and envelope.fallback_target:
            targets.add(("private", envelope.fallback_target))
        async with AsyncExitStack() as stack:
            for target in sorted(targets):
                await stack.enter_async_context(self._locks.setdefault(target, asyncio.Lock()))
            if key in self._uncertain:
                return self._uncertain[key]
            self._uncertain[key] = DeliveryResult(DeliveryStatus.UNKNOWN, reason="OneBot delivery interrupted")
            result = await self._deliver_locked(envelope)
            if result.status == DeliveryStatus.UNKNOWN:
                self._uncertain[key] = result
            else:
                self._uncertain.pop(key, None)
            return result

    @staticmethod
    def _is_rejection(error):
        return type(error.retcode) is int and error.retcode != 0

    async def _media_bytes(self, descriptor, cap, label):
        data = descriptor.get("data")
        try:
            if not data and descriptor.get("url"):
                data = await asyncio.to_thread(self.client.download_bytes, descriptor["url"], cap,
                                               self.config.download_timeout_s)
        except ValueError as exc:
            if "上限" in str(exc) or "exceeded limit" in str(exc):
                raise ValueError(f"OneBot {label} exceeds upload limit (>{cap} bytes)") from exc
            raise
        if not isinstance(data, (bytes, bytearray)) or not data:
            raise ValueError(f"OneBot {label} preparation failed")
        if len(data) > cap:
            raise ValueError(f"OneBot {label} exceeds upload limit ({len(data)} > {cap} bytes)")
        return data

    async def _deliver_locked(self, envelope):
        receipts, failures = [], []
        references = dict(envelope.quote.references) if envelope.quote else {}
        ref_id = references.get("id")
        has_native_quote = bool(references)

        def segments(segment, fallback=False):
            if ref_id:
                return [{"type": "reply", "data": {"id": ref_id}}, segment]
            prefix = (GROUP_FALLBACK_PREFIX if fallback else "") + envelope.quote_fallback
            return ([text_segment(prefix)] if prefix else []) + [segment]

        async def send(operation, *, allow_fallback=True):
            kind, target = envelope.target_type, envelope.target_id
            try:
                body = await asyncio.to_thread(operation, kind, target, False)
            except OneBotAPIError as exc:
                # _check also uses this error for malformed responses, which
                # cannot establish that the non-idempotent operation was refused.
                if not self._is_rejection(exc):
                    return DeliveryStatus.UNKNOWN, kind, target, None, "OneBot response outcome unknown"
                if (allow_fallback and kind == "group" and envelope.fallback_target
                        and not has_native_quote):
                    kind, target = "private", envelope.fallback_target
                    try:
                        body = await asyncio.to_thread(operation, kind, target, True)
                    except OneBotAPIError as fallback_exc:
                        status = (DeliveryStatus.REJECTED if self._is_rejection(fallback_exc)
                                  else DeliveryStatus.UNKNOWN)
                        return status, kind, target, None, "OneBot fallback not confirmed"
                    except Exception:
                        return DeliveryStatus.UNKNOWN, kind, target, None, "OneBot fallback outcome unknown"
                else:
                    return DeliveryStatus.REJECTED, kind, target, None, "OneBot send refused"
            except Exception:
                return DeliveryStatus.UNKNOWN, kind, target, None, "OneBot send outcome unknown"
            return DeliveryStatus.CONFIRMED, kind, target, body, ""

        async def message(segment):
            status, kind, target, body, reason = await send(
                lambda t, i, fallback: self.client.send_msg(t, i, segments(segment, fallback)))
            if status == DeliveryStatus.CONFIRMED:
                receipts.append(DeliveryReceipt.from_body(kind, target, body))
            else:
                failures.append(reason)
            return status

        for chunk in chunk_text(envelope.text, self.config.chunk_chars, self.config.max_chunks) if envelope.text else []:
            if await message(text_segment(chunk)) == DeliveryStatus.UNKNOWN:
                return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, failures[-1])

        for label, items, cap in (("image", envelope.images, self.config.max_image_bytes),
                                  ("video", envelope.videos, self.config.max_video_upload_bytes)):
            for descriptor in items:
                try:
                    data = await self._media_bytes(descriptor, cap, label)
                except Exception as exc:
                    msg = str(exc)
                    failures.append(msg if "exceeds upload limit" in msg else f"OneBot {label} preparation failed")
                    continue
                if await message(media_segment(label, data)) == DeliveryStatus.UNKNOWN:
                    return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, failures[-1])

        for descriptor in envelope.files:
            try:
                data = await self._media_bytes(descriptor, self.config.max_file_bytes, "file")
                name = descriptor.get("filename") or "file.dat"
            except Exception as exc:
                msg = str(exc)
                failures.append(msg if "exceeds upload limit" in msg else "OneBot file preparation failed")
                continue
            status, kind, target, body, reason = await send(
                lambda t, i, fallback: self.client.upload_file(t, i, data, name))
            if status != DeliveryStatus.CONFIRMED:
                failures.append(reason)
                if status == DeliveryStatus.UNKNOWN:
                    return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, reason)
                continue
            # upload_*_file is a real delivery even though it has no message ID.
            receipts.append(DeliveryReceipt.from_body(kind, target, body))
            # A companion supplies a native, replyable ID. If it fails, do not
            # repeat the already-confirmed upload or move the companion elsewhere.
            try:
                body = await asyncio.to_thread(self.client.send_msg, kind, target,
                                               segments(text_segment("【文件】" + name), kind != envelope.target_type))
            except OneBotAPIError as exc:
                if not self._is_rejection(exc):
                    return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, "OneBot file companion outcome unknown")
                failures.append("OneBot file companion refused")
            except Exception:
                return DeliveryResult(DeliveryStatus.UNKNOWN, receipts, "OneBot file companion outcome unknown")
            else:
                receipts.append(DeliveryReceipt.from_body(kind, target, body))

        if failures:
            return DeliveryResult(DeliveryStatus.PARTIAL if receipts else DeliveryStatus.REJECTED,
                                  receipts, "; ".join(dict.fromkeys(failures)))
        if not receipts:
            return DeliveryResult(DeliveryStatus.REJECTED, reason="Empty OneBot delivery")
        return DeliveryResult(DeliveryStatus.CONFIRMED, receipts)
