"""Quote protocol fixtures contain only fictional text and IDs."""
import json
import tempfile
import unittest
from unittest import mock
from test_onebot_adapter import _make_adapter, _group
from muse_bridge.core.message import DeliveryReference
from muse_bridge.muse.client.client import MuseClient
from muse_bridge.adapters.onebot.client import OneBotAPIError
from muse_bridge.adapters.qq.outbound import QQOutboundPusher
from muse_bridge.config.models import QQConfig
from muse_bridge.core.deduplicator import MessageDeduplicator
from muse_bridge.core.session_map import SessionMapper
from muse_bridge.gateway.routes import RouteManager


def assistant(mid='answer', reply=None, seq=2):
    return {'event_name': 'message.assistant', 'role': 'assistant', 'message_id': mid,
            'reply_to_message_id': reply, 'seq': seq, 'display_text': 'fictional answer',
            'payload': {'status': 'completed'}}


class ClientQuoteCase(unittest.TestCase):
    def test_serializes_quote_for_text_and_media(self):
        for media in (False, True):
            client = MuseClient.__new__(MuseClient)
            client.client_ip, client.timezone = '198.51.100.1', 'UTC'
            client.request = mock.Mock(return_value=b'{"message_id":"user-mid"}')
            kwargs = {'images': [{'data': b'fictional', 'mime_type': 'image/png'}]} if media else {}
            self.assertEqual(client.send_message('sid', 'reply', in_reply_to='old', **kwargs), 'user-mid')
            payload = json.loads(client.request.call_args.args[2])
            self.assertEqual(payload['in_reply_to'], 'old')
            self.assertEqual('items' in payload, media)

    def test_presentation_target_and_plain_compatibility(self):
        client = MuseClient.__new__(MuseClient)
        client.client_ip, client.timezone = '198.51.100.1', 'UTC'
        client.request = mock.Mock(return_value=b'{"message_id":"m"}')
        target = {'type': 'presentation', 'presentation_event_id': 'p'}
        client.send_message('s', 'reply', reply_to_target=target)
        self.assertEqual(json.loads(client.request.call_args.args[2])['reply_to_target'], target)
        client.send_message('s', 'plain')
        self.assertNotIn('in_reply_to', json.loads(client.request.call_args.args[2]))


class QuoteCase(unittest.IsolatedAsyncioTestCase):
    async def test_inbound_quote_roundtrip_and_session_isolation(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            muse.post_message.side_effect = None
            muse.post_message.return_value = 'user-mid'
            adapter.session_mapper.bind('group', 'G1', 'sid')
            store = adapter.outbound_pusher.reference_store
            store.remember('sid', 'group', 'G1', 'old-muse', [('id', '-7')], 'anchor')
            event = _group('G1', '100', 'reply', 88)
            event['message'].insert(0, {'type': 'reply', 'data': {'id': '-7'}})
            await adapter.handle_event(event)
            self.assertEqual(muse.post_message.call_args.kwargs, {'in_reply_to': 'old-muse'})
            self.assertEqual(store.resolve_muse('sid', 'group', 'G1', 'user-mid').references, [('id', '88')])
            adapter.session_mapper.bind('group', 'G1', 'new-sid')
            event['message_id'] = 89
            await adapter.handle_event(event)
            self.assertEqual(muse.post_message.call_args.kwargs, {})
            self.assertIn('未关联', muse.post_message.call_args.args[1])
    async def test_presentation_quote_and_at_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, group_trigger='at')
            muse.post_message.side_effect = None
            adapter.session_mapper.bind('group', 'G1', 'sid')
            target = {'type': 'presentation', 'presentation_event_id': 'card'}
            adapter.outbound_pusher.reference_store.remember('sid', 'group', 'G1', 'card', [('id', '7')], 'card', target)
            event = _group('G1', '100', 'reply', 90)
            event['message'].insert(0, {'type': 'reply', 'data': {'id': '7'}})
            await adapter.handle_event(event)
            self.assertEqual(muse.post_message.call_args.kwargs, {'reply_to_target': target})

    async def test_muse_quote_native_and_chunk_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp, chunk_chars=8)
            adapter.session_mapper.bind('group', 'G1', 'sid')
            pusher = adapter.outbound_pusher
            pusher.reference_store.remember('sid', 'group', 'G1', 'user', [('id', '-1')], 'anchor')
            adapter.client.send_msg.side_effect = [{'message_id': 10}, {'message_id': 11}, {'message_id': 12}]
            await pusher._scan_and_dispatch([assistant(reply='user')], 'sid')
            for call in adapter.client.send_msg.call_args_list:
                self.assertEqual(call.args[2][0], {'type': 'reply', 'data': {'id': '-1'}})
            record = pusher.reference_store.resolve_muse('sid', 'group', 'G1', 'answer')
            self.assertGreater(len(record.references), 1)
            for ref in record.references:
                self.assertEqual(pusher.reference_store.resolve_channel('sid', 'group', 'G1', [ref]).muse_id, 'answer')

    async def test_group_fallback_does_not_quote_foreign_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            adapter, muse = _make_adapter(tmp)
            p = adapter.outbound_pusher
            p.reference_store.remember('s', 'group', 'G1', 'old', [('id', '3')], 'group secret')
            adapter.client.send_msg.side_effect = [OneBotAPIError('send_group_msg', 1), {'message_id': 99}]
            success = await p._deliver('group', 'G1', 'reply', [], [], [], delivery=DeliveryReference('s', 'new', 'reply', 'old'))
            self.assertFalse(success)
            self.assertEqual(adapter.client.send_msg.call_count, 1)
            self.assertIsNone(p.reference_store.resolve_muse('s', 'private', 'A1', 'new'))

    async def test_qq_text_quote_and_opaque_reference(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = QQConfig(app_id='bot', app_secret='unused', admin_id=['admin'],
                              routes_file=tmp+'/routes', push_state_file=tmp+'/push')
            client = mock.Mock()
            client.send_message.return_value = (200, {'id': 'qq-out', 'ext_info': {'ref_idx': 'opaque'}})
            p = QQOutboundPusher(config, client, mock.Mock(), MessageDeduplicator(), RouteManager(), SessionMapper())
            p.reference_store.remember('s', 'group', 'G', 'old', [('ref', 'old-ref')], 'quoted anchor')
            await p._deliver('group', 'G', 'reply', [], [], [], delivery=DeliveryReference('s', 'new', 'reply', 'old'))
            self.assertEqual(client.send_message.call_args.args[2], 'reply')
            self.assertEqual(client.send_message.call_args.kwargs['message_reference'],
                             {'message_id': 'old-ref', 'ignore_get_message_error': False})
            self.assertIsNone(client.send_message.call_args.args[5])
            self.assertEqual(p.reference_store.resolve_channel('s', 'group', 'G', [('ref', 'opaque')]).muse_id, 'new')

    async def test_qq_missing_reference_only_uses_text_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = QQConfig(app_id='bot', app_secret='unused', routes_file=tmp+'/r', push_state_file=tmp+'/p')
            client = mock.Mock()
            client.send_message.return_value = (200, {'id': 'out'})
            p = QQOutboundPusher(config, client, mock.Mock(), MessageDeduplicator(), RouteManager(), SessionMapper())
            await p._deliver('group', 'G', 'reply', [], [], [], delivery=DeliveryReference('s', 'new', 'reply', 'missing'))
            self.assertIn('原消息在此聊天不可用', client.send_message.call_args.args[2])
            self.assertNotIn('message_reference', client.send_message.call_args.kwargs)

    async def test_qq_native_send_error_does_not_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = QQConfig(app_id='bot', app_secret='unused', routes_file=tmp+'/r', push_state_file=tmp+'/p')
            client = mock.Mock()
            client.send_message.return_value = (400, {'message': 'not allowed'})
            p = QQOutboundPusher(config, client, mock.Mock(), MessageDeduplicator(), RouteManager(), SessionMapper())
            p.reference_store.remember('s', 'group', 'G', 'old', [('ref', 'REFIDX_exact')])
            self.assertFalse(await p._deliver('group', 'G', 'reply', [], [], [], delivery=DeliveryReference('s', 'n', 'reply', 'old')))
            self.assertEqual(client.send_message.call_count, 1)
            self.assertEqual(client.send_message.call_args.kwargs['message_reference']['message_id'], 'REFIDX_exact')


    async def test_qq_permission_error_keeps_native_quote_without_admin_fallback(self):
        for error in ({'code': 40034105}, {'message': '无权限'}, {'message': '主动消息失败'}):
            for passive in (False, True):
                with self.subTest(error=error, passive=passive), tempfile.TemporaryDirectory() as tmp:
                    config = QQConfig(app_id='bot', app_secret='unused', admin_id=['admin'],
                                      routes_file=tmp+'/r', push_state_file=tmp+'/p')
                    client = mock.Mock()
                    client.send_message.return_value = (400, error)
                    routes = RouteManager(state_file=None)
                    route = routes.register('user', 'group', 'G', 'passive-id') if passive else None
                    p = QQOutboundPusher(config, client, mock.Mock(), MessageDeduplicator(), routes, SessionMapper())
                    p.reference_store.remember('s', 'group', 'G', 'old', [('ref', 'REFIDX_exact')])
                    result = await p._deliver('group', 'G', 'reply', [], [], [], route=route,
                                              delivery=DeliveryReference('s', 'new', 'reply', 'old'))
                    self.assertFalse(result)
                    self.assertEqual(client.send_message.call_count, 2 if passive else 1)
                    for call in client.send_message.call_args_list:
                        self.assertEqual(call.args[:3], ('group', 'G', 'reply'))
                        self.assertEqual(call.kwargs['message_reference'],
                                         {'message_id': 'REFIDX_exact', 'ignore_get_message_error': False})
                    self.assertIsNone(p.reference_store.resolve_muse('s', 'private', 'admin', 'new'))


class QQPayloadCase(unittest.TestCase):
    def test_native_reference_is_not_passive_authorization(self):
        from muse_bridge.adapters.qq.client import QQClient
        c = QQClient('test', 'unused')
        c.post_with_retry = mock.Mock(return_value=(200, {'id': 'out'}))
        ref = {'message_id': 'REFIDX_opaque', 'ignore_get_message_error': False}
        for kind in ('group', 'private'):
            c.send_message(kind, 'target', 'reply', msg_id='passive-id', msg_seq=3, message_reference=ref)
            payload = c.post_with_retry.call_args.args[1]
            self.assertEqual(payload['message_reference'], ref)
            self.assertEqual(payload['msg_id'], 'passive-id')
            self.assertEqual(payload['msg_seq'], 3)

class ReactionCase(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def update(seq=10, emojis=None):
        return {'event': 'reactions.updated', 'seq': seq,
                'payload': {'session_id': 's', 'message_id': 'old',
                            'reactions': [{'emoji': e} for e in (emojis if emojis is not None else ['👍'])]}}

    async def test_live_reaction_on_old_message_is_quoted_and_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            a.session_mapper.bind('group', 'G1', 's')
            p = a.outbound_pusher
            p.reference_store.remember('s', 'group', 'G1', 'old', [('id', '-99')], 'old anchor')
            p._seen_ids.add('old')
            p.deduplicator.mark_delivered(['old'])
            await p._scan_and_dispatch([self.update()], 's')
            call = a.client.send_msg.call_args
            self.assertEqual(call.args[2][0], {'type': 'reply', 'data': {'id': '-99'}})
            self.assertIn('👍', str(call.args[2]))
            await p._scan_and_dispatch([self.update(), self.update(9, ['❤️'])], 's')
            self.assertEqual(a.client.send_msg.call_count, 1)
            await p._scan_and_dispatch([self.update(11, ['❤️'])], 's')
            self.assertEqual(a.client.send_msg.call_count, 2)
            self.assertEqual(a.client.send_msg.call_args.args[2][1]['data']['text'], chr(10).join(['❤️', '👍']))
            await p._scan_and_dispatch([self.update(12, [])], 's')
            self.assertEqual(p.reference_store.reaction_state('s', 'old'), (12, []))

    async def test_history_user_reaction_runs_before_role_dedup(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            a.session_mapper.bind('group', 'G1', 's')
            p = a.outbound_pusher
            p.reference_store.remember('s', 'group', 'G1', 'old', [('id', '8')])
            event = {'event_name': 'message.user', 'role': 'user', 'message_id': 'old',
                     'seq': 1, 'reaction_emoji': '❤️'}
            await p._scan_and_dispatch([event], 's')
            self.assertIn('❤️', str(a.client.send_msg.call_args))
            await p._scan_and_dispatch([event], 's')
            self.assertEqual(a.client.send_msg.call_count, 1)

    async def test_startup_baseline_does_not_replay_reactions(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            a.session_mapper.bind('group', 'G1', 's')
            p = a.outbound_pusher
            p.config.catch_up_enabled = False
            event = {'event_name': 'message.user', 'role': 'user', 'message_id': 'old',
                     'seq': 1, 'reaction_emoji': '👍'}
            await p._hydrate_and_dispatch('s', [event], True)
            a.client.send_msg.assert_not_called()
            await p._scan_and_dispatch([self.update(10, ['❤️'])], 's')
            self.assertEqual(a.client.send_msg.call_count, 1)
            self.assertEqual(p._classify_push_event(self.update()), ('s', 'sync'))

    async def test_failed_reaction_delivery_keeps_retryable_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            a.session_mapper.bind('private', '100', 's')
            a.client.send_msg.side_effect = OneBotAPIError('send_private_msg', 1)
            p = a.outbound_pusher
            await p._scan_and_dispatch([self.update()], 's')
            self.assertIsNone(p.reference_store.reaction_state('s', 'old'))
            a.client.send_msg.side_effect = None
            await p._scan_and_dispatch([self.update()], 's')
            self.assertEqual(p.reference_store.reaction_state('s', 'old'), (10, ['👍']))


class QQReactionTextCase(unittest.IsolatedAsyncioTestCase):
    def make_pusher(self, tmp, kind='private'):
        from muse_bridge.adapters.qq.client import QQClient
        config = QQConfig(app_id='bot', app_secret='unused', admin_id=['admin'],
                          routes_file=tmp+'/r', push_state_file=tmp+'/p')
        client = QQClient('bot', 'unused')
        client.post_with_retry = mock.Mock(return_value=(200, {'id': 'out'}))
        mapper = SessionMapper(bindings={kind+':target': 's'})
        pusher = QQOutboundPusher(config, client, mock.Mock(), MessageDeduplicator(persistence_file=None),
                                  RouteManager(state_file=None), mapper)
        pusher.reference_store.remember('s', kind, 'target', 'old', [('ref', 'REFIDX_old')])
        return pusher, client

    async def test_reaction_uses_text_with_native_quote_and_deduplicates(self):
        for kind in ('private', 'group'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                p, client = self.make_pusher(tmp, kind)
                await p._scan_and_dispatch([ReactionCase.update()], 's')
                client.post_with_retry.assert_called_once()
                payload = client.post_with_retry.call_args.args[1]
                self.assertEqual(payload['content'], '👍')
                self.assertEqual(payload.get('msg_type', 0), 0)
                self.assertNotIn('markdown', payload)
                self.assertEqual(payload['message_reference'],
                                 {'message_id': 'REFIDX_old', 'ignore_get_message_error': False})
                await p._scan_and_dispatch([ReactionCase.update()], 's')
                client.post_with_retry.assert_called_once()
                await p._scan_and_dispatch([ReactionCase.update(11, [])], 's')
                payload = client.post_with_retry.call_args.args[1]
                self.assertEqual(payload['content'], '👍')
                self.assertNotIn('markdown', payload)
                self.assertEqual(payload['message_reference']['message_id'], 'REFIDX_old')
                self.assertEqual(p.reference_store.reaction_state('s', 'old'), (11, []))

    async def test_plain_text_is_kept_during_passive_active_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, client = self.make_pusher(tmp)
            client.post_with_retry.side_effect = [(400, {'message': 'expired'}), (200, {'id': 'out'})]
            route = p.route_manager.register('user', 'private', 'target', 'passive-id')
            result = await p._deliver('private', 'target', '👍', [], [], [], route=route,
                                      delivery=DeliveryReference('s', 'old', reply_to_id='old', plain_text=True))
            self.assertTrue(result)
            self.assertEqual(client.post_with_retry.call_count, 2)
            first, second = [call.args[1] for call in client.post_with_retry.call_args_list]
            for payload in (first, second):
                self.assertEqual(payload['content'], '👍')
                self.assertNotIn('markdown', payload)
                self.assertEqual(payload['message_reference']['message_id'], 'REFIDX_old')
            self.assertEqual(first['msg_id'], 'passive-id')
            self.assertNotIn('msg_id', second)

    async def test_missing_original_reaction_fallback_remains_plain_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            p, client = self.make_pusher(tmp)
            event = ReactionCase.update()
            event['payload']['message_id'] = 'missing'
            await p._scan_and_dispatch([event], 's')
            payload = client.post_with_retry.call_args.args[1]
            self.assertIn('原消息在此聊天不可用', payload['content'])
            self.assertTrue(payload['content'].endswith('👍'))
            self.assertNotIn('markdown', payload)
            self.assertNotIn('message_reference', payload)

    async def test_ordinary_replies_keep_markdown_with_or_without_quote(self):
        for quoted in (False, True):
            with self.subTest(quoted=quoted), tempfile.TemporaryDirectory() as tmp:
                p, client = self.make_pusher(tmp)
                event = assistant(reply='old' if quoted else None)
                await p._scan_and_dispatch([event], 's')
                client.post_with_retry.assert_called_once()
                payload = client.post_with_retry.call_args.args[1]
                self.assertEqual(payload['msg_type'], 2)
                self.assertEqual(payload['markdown'], {'content': 'fictional answer'})
                self.assertNotIn('content', payload)
                self.assertEqual('message_reference' in payload, quoted)


class SelfReferenceCase(unittest.IsolatedAsyncioTestCase):
    async def test_event_references_on_qq_and_onebot(self):
        cases = [
            ('exact-self', 'answer', 'answer', {}, None),
            ('bare-self', 'assistant-msg-answer', 'answer', {}, None),
            ('prefixed-self', 'answer', 'assistant-msg-answer', {}, None),
            ('payload-self', 'assistant-msg-answer', None,
             {'message_id': 'assistant-msg-answer', 'reply_to_message_id': 'answer'}, None),
            ('both-self', 'assistant-msg-answer', 'answer',
             {'message_id': 'assistant-msg-answer', 'reply_to_message_id': 'answer',
              'reply_to_target': None}, None),
            ('payload-identity', 'carrier', 'answer', {'message_id': 'assistant-msg-answer'}, None),
            ('explicit-self', 'assistant-msg-answer', None,
             {'reply_to_target': {'type': 'message', 'message_id': 'answer'}}, None),
            ('known-message', 'answer', 'old', {}, 'old'),
            ('missing-message', 'answer', 'missing', {}, 'missing'),
            ('self-then-payload', 'assistant-msg-answer', 'answer',
             {'reply_to_message_id': 'old'}, 'old'),
            ('self-then-explicit', 'assistant-msg-answer', 'answer',
             {'reply_to_message_id': 'answer',
              'reply_to_target': {'type': 'message', 'message_id': 'old'}}, 'old'),
            ('self-then-missing', 'assistant-msg-answer', 'answer',
             {'reply_to_target': {'type': 'message', 'message_id': 'missing'}}, 'missing'),
            ('shared-root-is-real-quote', 'answer', 'old',
             {'metadata': {'root_message_execution_id': 'old'}}, 'old'),
            ('presentation', 'assistant-msg-answer', 'answer',
             {'reply_to_target': {'type': 'presentation', 'presentation_event_id': 'card'}}, 'card'),
            ('presentation-distinct-namespace', 'assistant-msg-answer', 'answer',
             {'reply_to_target': {'type': 'presentation', 'presentation_event_id': 'answer'}}, 'answer'),
        ]
        for platform in ('qq', 'onebot'):
            for name, mid, reply, payload_fields, expected in cases:
                with self.subTest(platform=platform, case=name), tempfile.TemporaryDirectory() as tmp:
                    if platform == 'qq':
                        p, client = QQReactionTextCase.make_pusher(self, tmp, 'group')
                        target_id, reference = 'target', ('ref', 'REFIDX_old')
                    else:
                        adapter, _ = _make_adapter(tmp)
                        adapter.session_mapper.bind('group', 'G1', 's')
                        p, client = adapter.outbound_pusher, adapter.client
                        target_id, reference = 'G1', ('id', '8')
                        p.reference_store.remember('s', 'group', target_id, 'old', [reference])
                    for card_id in ('card', 'answer'):
                        p.reference_store.remember(
                            's', 'group', target_id, 'carrier-' + card_id,
                            [(reference[0], 'REFIDX_' + card_id if platform == 'qq' else 'card-' + card_id)],
                            reply_target={'type': 'presentation', 'presentation_event_id': card_id})
                    event = assistant(mid, reply)
                    event['payload'].update(payload_fields)
                    await p._scan_and_dispatch([event], 's')
                    expected_ref = reference[1]
                    if expected in ('card', 'answer'):
                        expected_ref = 'REFIDX_' + expected if platform == 'qq' else 'card-' + expected
                    if platform == 'qq':
                        client.post_with_retry.assert_called_once()
                        sent = client.post_with_retry.call_args.args[1]
                        text = sent['markdown']['content']
                        native_reference = sent.get('message_reference')
                        if expected and expected != 'missing':
                            self.assertEqual(native_reference, {
                                'message_id': expected_ref, 'ignore_get_message_error': False})
                        else:
                            self.assertIsNone(native_reference)
                    else:
                        client.send_msg.assert_called_once()
                        segments = client.send_msg.call_args.args[2]
                        text = ''.join(seg['data']['text'] for seg in segments if seg['type'] == 'text')
                        replies = [seg for seg in segments if seg['type'] == 'reply']
                        self.assertEqual(replies, [{'type': 'reply', 'data': {'id': expected_ref}}]
                                         if expected and expected != 'missing' else [])
                    if expected == 'missing':
                        self.assertIn('原消息在此聊天不可用', text)
                        self.assertTrue(text.endswith('fictional answer'))
                    else:
                        self.assertEqual(text, 'fictional answer')


class QQInboundCase(unittest.IsolatedAsyncioTestCase):
    async def test_qq_self_index_and_quote_are_kept_separate(self):
        from test_interactions import TestQQInteractions
        fixture = TestQQInteractions()
        fixture.setUp()
        try:
            adapter = fixture.adapter
            adapter.session_mapper.bind('private', 'ADMIN', 's1')
            p = adapter.outbound_pusher
            p.reference_store.remember('s1', 'private', 'ADMIN', 'old-muse', [('ref', 'REFIDX_OLD==')])
            await adapter.handle_event('C2C_MESSAGE_CREATE', {
                'id': 'QQ-NEW', 'content': 'quote test', 'author': {'user_openid': 'ADMIN'},
                'message_scene': {'ext': ['ref_msg_idx=REFIDX_OLD==', 'msg_idx=REFIDX_SELF==']}})
            self.assertEqual(fixture.service.post_message.call_args.kwargs, {'in_reply_to': 'old-muse'})
            sent_mid = fixture.service.post_message.return_value
            own = p.reference_store.resolve_channel('s1', 'private', 'ADMIN', [('ref', 'REFIDX_SELF==')])
            self.assertEqual(own.muse_id, str(sent_mid))
            self.assertNotIn(('ref', 'REFIDX_OLD=='), own.references)
        finally:
            fixture.tearDown()

class QuoteFailureCase(unittest.IsolatedAsyncioTestCase):
    async def test_missing_muse_quote_does_not_recreate_or_repost(self):
        from muse_bridge.muse.client.errors import RPCError
        with tempfile.TemporaryDirectory() as tmp:
            a, service = _make_adapter(tmp)
            a.session_mapper.bind('group', 'G1', 's')
            a.outbound_pusher.reference_store.remember('s', 'group', 'G1', 'old', [('id', '7')])
            service.post_message.side_effect = RPCError('POST', '/chat/stream', 404, b'message_not_found')
            event = _group('G1', '100', 'reply', 101)
            event['message'].insert(0, {'type': 'reply', 'data': {'id': '7'}})
            await a.handle_event(event)
            self.assertEqual(a.session_mapper.current('group', 'G1'), 's')
            self.assertEqual(service.post_message.call_count, 1)

    async def test_history_reconciles_reaction_missed_after_live_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            a.session_mapper.bind('group', 'G1', 's')
            p = a.outbound_pusher
            await p._scan_and_dispatch([ReactionCase.update(10, ['👍'])], 's')
            event = {'event_name': 'message.user', 'role': 'user', 'message_id': 'old',
                     'seq': 1, 'reaction_emoji': '❤️'}
            await p._scan_and_dispatch([event], 's')
            self.assertEqual(p.reference_store.reaction_state('s', 'old'), (10, ['❤️']))
            self.assertEqual(a.client.send_msg.call_count, 2)

class DeliveryEdgeCase(unittest.IsolatedAsyncioTestCase):
    async def test_presentation_event_id_alias_resolves_original_delivery(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            p = a.outbound_pusher
            p.reference_store.remember('s', 'group', 'G1', 'carrier-mid', [('id', '8')],
                                       reply_target={'type': 'presentation', 'presentation_event_id': 'presentation-id'})
            rec = p.quote_for(DeliveryReference('s', 'reply', reply_to_id='presentation-id'), 'group', 'G1')
            self.assertEqual(rec.muse_id, 'carrier-mid')

    async def test_account_switch_drops_previous_bot_reference_ids(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            p = a.outbound_pusher
            p.reference_store.remember('s', 'group', 'G1', 'old', [('id', '8')])
            p.bind_reference_account('different-bot')
            self.assertIsNone(p.reference_store.resolve_channel('s', 'group', 'G1', [('id', '8')]))

    async def test_file_companion_retains_native_quote_and_reverse_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            a, _ = _make_adapter(tmp)
            p = a.outbound_pusher
            p.reference_store.remember('s', 'group', 'G1', 'old', [('id', '8')])
            result = await p._deliver('group', 'G1', '', [], [{'data': b'fictional', 'filename': 'test.txt'}], [],
                                      delivery=DeliveryReference('s', 'file-mid', reply_to_id='old'))
            self.assertTrue(result)
            a.client.upload_file.assert_called_once()
            self.assertEqual(a.client.send_msg.call_args.args[2][0], {'type': 'reply', 'data': {'id': '8'}})
            self.assertEqual(p.reference_store.resolve_channel('s', 'group', 'G1', [('id', '777')]).muse_id, 'file-mid')

    async def test_qq_media_missing_quote_keeps_fallback_caption(self):
        from muse_bridge.adapters.qq.client import QQClient
        c = QQClient('test', 'unused')
        c.post_with_retry = mock.Mock(return_value=(200, {}))
        c.send_message('group', 'G', '【引用】原消息在此聊天不可用', media_file_info='opaque')
        self.assertIn('【引用】', c.post_with_retry.call_args.args[1]['content'])
