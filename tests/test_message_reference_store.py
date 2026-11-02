"""Tests for scoped message references and persistent reaction snapshots."""

import dataclasses
import json
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

from muse_bridge.core.references import MessageReferenceStore


class TestMessageReferenceStore(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_file = os.path.join(self._tmp.name, "nested", "references.json")
        self.store = MessageReferenceStore(self.state_file, identity="bot-a")

    def reload(self, **kwargs):
        return MessageReferenceStore(self.state_file, identity="bot-a", **kwargs)

    def remember(self, mid="m1", refs=None, **kwargs):
        return self.store.remember("s1", "group", "g1", mid,
                                   [("id", "channel-1")] if refs is None else refs, **kwargs)

    def resolve(self, refs):
        return self.store.resolve_channel("s1", "group", "g1", refs)

    def write_state(self, data):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_record_and_public_fields(self):
        record = self.remember(excerpt="a short excerpt")
        self.assertTrue(dataclasses.is_dataclass(record))
        self.assertEqual(dataclasses.asdict(record), {
            "session_id": "s1", "target_type": "group", "target_id": "g1",
            "muse_id": "m1", "references": [("id", "channel-1")],
            "excerpt": "a short excerpt", "reply_target": None,
        })
        self.assertEqual(self.resolve([("id", "channel-1")]), record)
        self.assertEqual(self.store.resolve_muse("s1", "group", "g1", "m1"), record)

    def test_persistence_and_presentation_allowlist(self):
        record = self.remember(
            refs=[("id", "channel-1"), ("ref", "opaque/001")], excerpt="文" * 300,
            reply_target={"type": "presentation", "presentation_event_id": -7,
                          "raw_event": {"secret": "not-persisted"}},
        )
        self.assertEqual(record.excerpt, "文" * 240)
        self.assertEqual(record.reply_target, {"type": "presentation", "presentation_event_id": "-7"})
        loaded = self.reload()
        self.assertEqual(loaded.resolve_muse("s1", "group", "g1", "m1"), record)
        self.assertEqual(loaded.resolve_channel("s1", "group", "g1", [("ref", "opaque/001")]), record)
        with open(self.state_file, encoding="utf-8") as f:
            text = f.read()
        self.assertNotIn("raw_event", text)
        self.assertNotIn("not-persisted", text)
        self.assertEqual(os.listdir(os.path.dirname(self.state_file)), ["references.json"])

    def test_scope_isolates_session_chat_and_type(self):
        record = self.remember(excerpt="private to this scope")
        for scope in [("s2", "group", "g1"), ("s1", "group", "g2"), ("s1", "private", "g1")]:
            with self.subTest(scope=scope):
                self.assertIsNone(self.store.resolve_channel(*scope, [("id", "channel-1")]))
                self.assertIsNone(self.store.resolve_muse(*scope, "m1"))
                other = self.store.remember(*scope, "m1", [("id", "channel-1")], excerpt="other")
                self.assertEqual(self.store.resolve_muse(*scope, "m1"), other)
        self.assertEqual(self.resolve([("id", "channel-1")]), record)

    def test_tuple_keys_do_not_collide_on_delimiters(self):
        a = self.store.remember("s:a", "group", "b", "m", [("id", "x")])
        b = self.store.remember("s", "a:group", "b", "m", [("id", "x")])
        self.assertNotEqual(a, b)
        self.assertEqual(self.store.resolve_channel("s:a", "group", "b", [("id", "x")]), a)

    def test_bot_identity_rejects_all_persisted_state(self):
        self.remember()
        self.store.set_reaction_state("s1", "m1", 3, ["👍"])
        other = MessageReferenceStore(self.state_file, identity="bot-b")
        self.assertIsNone(other.resolve_muse("s1", "group", "g1", "m1"))
        self.assertIsNone(other.resolve_channel("s1", "group", "g1", [("id", "channel-1")]))
        self.assertIsNone(other.reaction_state("s1", "m1"))
        self.assertIsNone(MessageReferenceStore(self.state_file).reaction_state("s1", "m1"))
        self.assertIsNotNone(self.reload().reaction_state("s1", "m1"))

    def test_opaque_numeric_negative_and_whitespace_ids(self):
        record = self.store.remember(-1, "group", -2, -3,
                                     ((kind, value) for kind, value in
                                      [("id", -4), ("ref", "00012"), ("id", " a:b/☃ "), ("ref", 0)]))
        self.assertEqual(record.references, [("id", "-4"), ("ref", "00012"),
                                             ("id", " a:b/☃ "), ("ref", "0")])
        for ref in record.references:
            self.assertEqual(self.reload().resolve_channel(-1, "group", -2, [ref]), record)
        self.assertIsNone(self.store.resolve_channel(-1, "group", -2, [("ref", "12")]))

    def test_reference_kinds_are_distinct_namespaces(self):
        a = self.remember(refs=[("id", "same")])
        b = self.remember("m2", [("ref", "same")])
        self.assertEqual(self.resolve([("id", "same")]), a)
        self.assertEqual(self.resolve([("ref", "same")]), b)
        self.assertIsNone(self.resolve([("id", "same"), ("ref", "same")]))

    def test_chunk_merge_deduplicates_and_keeps_metadata(self):
        self.remember(refs=[("id", "a"), ("ref", "b")], excerpt="summary",
                      reply_target={"type": "presentation", "presentation_event_id": "p"})
        record = self.remember(refs=iter([("id", "c"), ("id", "a")]))
        self.assertEqual(record.references, [("ref", "b"), ("id", "c"), ("id", "a")])
        self.assertEqual(record.excerpt, "summary")
        self.assertEqual(record.reply_target["presentation_event_id"], "p")
        for ref in record.references:
            self.assertEqual(self.resolve([ref]), record)
        self.assertEqual(self.reload().resolve_muse("s1", "group", "g1", "m1"), record)

    def test_latest_metadata_replaces_previous(self):
        self.remember(excerpt="first", reply_target={"type": "presentation", "presentation_event_id": "p1"})
        record = self.remember(excerpt="last", reply_target={"type": "presentation", "presentation_event_id": "p2"})
        self.assertEqual(record.excerpt, "last")
        self.assertEqual(record.reply_target["presentation_event_id"], "p2")

    def test_unknown_extra_references_are_ignored(self):
        record = self.remember()
        self.assertEqual(self.resolve([("id", "unknown"), ("id", "channel-1")]), record)
        self.assertIsNone(self.resolve([("id", "unknown")]))
        self.assertIsNone(self.resolve([]))
        self.assertIsNone(self.store.resolve_muse("s1", "group", "g1", "missing"))

    def test_ambiguous_and_conflicting_references_fail_closed(self):
        self.remember(refs=[("id", "shared"), ("ref", "a")])
        self.remember("m2", [("id", "shared"), ("ref", "b")])
        for refs in [[("id", "shared")], [("ref", "a"), ("ref", "b")],
                     [("id", "shared"), ("ref", "a")]]:
            with self.subTest(refs=refs):
                self.assertIsNone(self.resolve(refs))
                self.assertIsNone(self.reload().resolve_channel("s1", "group", "g1", refs))

    def test_conflict_before_many_unknown_refs_is_not_truncated(self):
        self.remember(refs=[("id", "a")])
        self.remember("m2", [("id", "b")])
        refs = [("id", "a")] + [("ref", str(i)) for i in range(300)] + [("id", "b")]
        self.assertIsNone(self.resolve(refs))

    def test_record_bound_refreshes_on_remember_not_lookup(self):
        self.store = MessageReferenceStore(self.state_file, identity="bot-a", max_records=2)
        self.remember("m1", [("id", "a")])
        self.remember("m2", [("id", "b")])
        self.remember("m1", [("id", "c")])
        self.resolve([("id", "b")])
        self.remember("m3", [("id", "d")])
        self.assertIsNone(self.resolve([("id", "b")]))
        self.assertIsNotNone(self.resolve([("id", "a")]))
        loaded = self.reload(max_records=1)
        self.assertIsNone(loaded.resolve_muse("s1", "group", "g1", "m1"))
        self.assertIsNotNone(loaded.resolve_muse("s1", "group", "g1", "m3"))

    def test_references_per_record_are_bounded(self):
        record = self.remember(refs=[("id", str(i)) for i in range(300)])
        self.assertEqual(len(record.references), 256)
        self.assertEqual(record.references[0], ("id", "44"))
        self.assertIsNone(self.resolve([("id", "0")]))
        record = self.remember(refs=[("id", "new")])
        self.assertEqual(len(record.references), 256)
        self.assertEqual(record.references[-1], ("id", "new"))

    def test_bad_reference_and_reply_payloads_are_not_persisted(self):
        record = self.remember(refs=[None, "id", ["id"], ("bad", "value"),
                                     ("id", {}), ("ref", True), ("id", ""), ("id", "ok")],
                               excerpt={"raw_event": "bad"}, reply_target={"type": "raw_event", "body": "bad"})
        self.assertEqual(record.references, [("id", "ok")])
        self.assertEqual(record.excerpt, "")
        self.assertIsNone(record.reply_target)

    def test_return_values_and_input_lists_are_detached(self):
        refs = [("id", "a")]
        target = {"type": "presentation", "presentation_event_id": "p"}
        record = self.remember(refs=refs, reply_target=target)
        refs.append(("id", "bad"))
        target["presentation_event_id"] = "bad"
        record.references.clear()
        record.reply_target.clear()
        for fetched in [self.resolve([("id", "a")]),
                        self.store.resolve_muse("s1", "group", "g1", "m1")]:
            self.assertEqual(fetched.references, [("id", "a")])
            self.assertEqual(fetched.reply_target["presentation_event_id"], "p")
            fetched.references.clear()
            fetched.reply_target.clear()
        self.assertEqual(self.resolve([("id", "a")]).references, [("id", "a")])

    def test_malformed_json_and_non_object_state_are_ignored(self):
        os.makedirs(os.path.dirname(self.state_file), exist_ok=True)
        for text in ["{broken", "null", "[]", '"text"',
                     '{"identity": "bot-a", "records": {}, "reactions": []}']:
            with self.subTest(text=text):
                with open(self.state_file, "w", encoding="utf-8") as f:
                    f.write(text)
                loaded = self.reload()
                self.assertIsNone(loaded.resolve_muse("s1", "group", "g1", "m1"))
                self.assertIsNone(loaded.reaction_state("s1", "m1"))
        with open(self.state_file, "wb") as f:
            f.write(b"\xff\xfe")
        self.assertIsNone(self.reload().reaction_state("s1", "m1"))

    def test_malformed_entries_do_not_discard_valid_neighbors(self):
        self.write_state({"identity": "bot-a", "records": [
            None, {}, {"session_id": []},
            {"session_id": "s1", "target_type": "group", "target_id": "g1",
             "muse_id": "m1", "references": [["id", "a"], ["id", {}]],
             "excerpt": "x" * 400, "reply_target": {"raw_event": {"bad": True}}},
        ], "reactions": [None, {},
            {"session_id": "s1", "message_id": "bad", "seq": True, "emojis": []},
            {"session_id": "s1", "message_id": "m1", "seq": 2, "emojis": ["👍", {}]},
        ]})
        loaded = self.reload()
        record = loaded.resolve_channel("s1", "group", "g1", [("id", "a")])
        self.assertEqual(record.excerpt, "x" * 240)
        self.assertIsNone(record.reply_target)
        self.assertEqual(loaded.reaction_state("s1", "m1"), (2, ["👍"]))
        self.assertIsNone(loaded.reaction_state("s1", "bad"))

    def test_missing_identity_does_not_load_default_store(self):
        self.write_state({"records": [], "reactions": [
            {"session_id": "s", "message_id": "m", "seq": 1, "emojis": ["👍"]},
        ]})
        self.assertIsNone(MessageReferenceStore(self.state_file).reaction_state("s", "m"))

    def test_reaction_state_persists_and_is_session_scoped(self):
        self.assertIsNone(self.store.reaction_state("s1", "m1"))
        self.store.set_reaction_state("s1", "m1", 7, ["👍", "👨‍👩‍👧‍👦"])
        self.store.set_reaction_state("s2", "m1", 2, ["❤️"])
        self.assertEqual(self.reload().reaction_state("s1", "m1"), (7, ["👍", "👨‍👩‍👧‍👦"]))
        self.assertEqual(self.reload().reaction_state("s2", "m1"), (2, ["❤️"]))
        self.assertIsNone(self.reload().reaction_state("s1", "other"))
        self.store.set_reaction_state("s1", "m1", 8, [])
        self.assertEqual(self.reload().reaction_state("s1", "m1"), (8, []))

    def test_reaction_inputs_and_returns_are_detached_and_sanitized(self):
        emojis = ["👍", {"raw_event": "bad"}, 1, "", "x" * 65]
        self.store.set_reaction_state(-1, -2, -3, emojis)
        emojis.append("bad")
        state = self.store.reaction_state("-1", "-2")
        self.assertEqual(state, (-3, ["👍"]))
        state[1].clear()
        self.assertEqual(self.reload().reaction_state(-1, -2), (-3, ["👍"]))
        for invalid in [True, "1", 1.5, None, {}]:
            with self.subTest(seq=invalid), self.assertRaises(ValueError):
                self.store.set_reaction_state(-1, -2, invalid, [])

    def test_reaction_bounds_and_latest_order_are_independent(self):
        self.store = MessageReferenceStore(self.state_file, identity="bot-a", max_records=2)
        record = self.remember()
        self.store.set_reaction_state("s1", "a", 1, ["👍"] * 100)
        self.store.set_reaction_state("s1", "b", 2, [])
        self.store.set_reaction_state("s1", "a", 3, ["👍"] * 100)
        self.store.set_reaction_state("s1", "c", 4, ["❤️"])
        self.assertEqual(len(self.store.reaction_state("s1", "a")[1]), 64)
        self.assertIsNone(self.store.reaction_state("s1", "b"))
        self.assertEqual(self.resolve([("id", "channel-1")]), record)
        loaded = self.reload(max_records=1)
        self.assertIsNone(loaded.reaction_state("s1", "a"))
        self.assertEqual(loaded.reaction_state("s1", "c"), (4, ["❤️"]))

    def test_zero_capacity_and_invalid_capacity(self):
        self.store = MessageReferenceStore(self.state_file, identity="bot-a", max_records=0)
        self.assertEqual(self.remember().muse_id, "m1")
        self.store.set_reaction_state("s1", "m1", 1, ["👍"])
        self.assertIsNone(self.resolve([("id", "channel-1")]))
        self.assertIsNone(self.reload().reaction_state("s1", "m1"))
        for invalid in [-1, True, 1.5, "2"]:
            with self.subTest(max_records=invalid), self.assertRaises(ValueError):
                MessageReferenceStore(max_records=invalid)

    def test_atomic_replace_failure_preserves_previous_file(self):
        first = self.remember()
        with mock.patch("muse_bridge.core.references.os.replace", side_effect=OSError("unavailable")):
            second = self.remember("m2", [("id", "two")])
        self.assertEqual(self.resolve([("id", "two")]), second)
        loaded = self.reload()
        self.assertEqual(loaded.resolve_muse("s1", "group", "g1", "m1"), first)
        self.assertIsNone(loaded.resolve_muse("s1", "group", "g1", "m2"))
        self.assertEqual(os.listdir(os.path.dirname(self.state_file)), ["references.json"])

    def test_unwritable_state_keeps_working_in_memory(self):
        self.store = MessageReferenceStore(self._tmp.name, identity="bot-a")
        record = self.remember()
        self.assertEqual(self.resolve([("id", "channel-1")]), record)
        self.store.set_reaction_state("s1", "m1", 1, ["👍"])
        self.assertEqual(self.store.reaction_state("s1", "m1"), (1, ["👍"]))

    def test_concurrent_chunk_merges_and_persistence(self):
        def update(index):
            self.remember(refs=[("id", str(index))])
            self.store.set_reaction_state("s1", str(index), index, ["👍"])
            self.assertIsNotNone(self.resolve([("id", str(index))]))

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(update, range(32)))
        loaded = self.reload()
        record = loaded.resolve_muse("s1", "group", "g1", "m1")
        self.assertEqual(set(record.references), {("id", str(i)) for i in range(32)})
        for i in range(32):
            self.assertEqual(loaded.reaction_state("s1", str(i)), (i, ["👍"]))


if __name__ == "__main__":
    unittest.main()
