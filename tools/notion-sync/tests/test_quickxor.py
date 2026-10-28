# -*- coding: utf-8 -*-
"""QuickXorHash against rclone v1.68.2's published vectors (backend/onedrive/quickxorhash) and a
byte-at-a-time reference, including read-chunk boundaries."""
import os, sys, base64, random, tempfile, unittest
from unittest import mock

CODE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, CODE_DIR)
from ci import graph  # noqa: E402

VECTORS = [  # (base64 input, expected) copied from rclone's quickxorhash_test.go
    ("", "AAAAAAAAAAAAAAAAAAAAAAAAAAA="),
    ("Sg==", "SgAAAAAAAAAAAAAAAQAAAAAAAAA="),
    ("tbQ=", "taAFAAAAAAAAAAAAAgAAAAAAAAA="),
    ("0pZP", "0rDEEwAAAAAAAAAAAwAAAAAAAAA="),
    ("2fuxhBJXtpWFe8dOfdGeHw==", "O9tHLAghgSvYohKFyMMxnNCHaHg="),
    ("T6LYJIfDh81JrAK309H2JMJTXis=", "zBTHrspn3mEcohlJdIUAbjGNaNg="),
    ("DWAAX5/CIfrmErgZa8ot6ZraeSbu", "LR2Z0PjuRYGKQB/mhQAuMrAGZbQ="),
    ("Mb7EGva2rEE5fENDL85P+BsapHEEjv2/siVhKjvAQe02feExVOQSkfmuYzU/kTF1MaKjPmKF/w+cbvwfdWL8aQ==",
     "n1anP5NfvD4XDYWIeRPW3ZkPv1Y="),
    ("jyibxJSzO6ZiZ0O1qe3tG/bvIAYssvukh9suIT5wEy1JBINVgPiqdsTW0cOpP0aUfP7mgqLfADkzI/m/GgCuVhr8oFLrOCoTx1/psBOWwhltCbhUx51Icm9aH8tY4Z3ccU+6BKpYQkLCy0B/A9Zc",
     "hZfLIilSITC6N3e3tQ/iSgEzkto="),
    ("PfxMcUd0vIW6VbHG/uj/Y0W6qEoKmyBD0nYebEKazKaKG+UaDqBEcmQjbfQeVnVLuodMoPp7P7TR1htX5n2VnkHh22xDyoJ8C/ZQKiSNqQfXvh83judf4RVr9exJCud8Uvgip6aVZTaPrJHVjQhMCp/dEnGvqg0oN5OVkM2qqAXvA0teKUDhgNM71sDBVBCGXxNOR2bpbD1iM4dnuT0ey4L+loXEHTL0fqMeUcEi2asgImnlNakwenDzz0x57aBwyq3AspCFGB1ncX4yYCr/OaCcS5OKi/00WH+wNQU3",
     "QX/YEpG0gDsmhEpCdWhsxDzsfVE="),
    ("w0LGhqU1WXFbdavqDE4kAjEzWLGGzmTNikzqnsiXHx2KRReKVTxkv27u3UcEz9+lbMvYl4xFf2Z4aE1xRBBNd1Ke5C0zToSaYw5o4B/7X99nKK2/XaUX1byLow2aju2XJl2OpKpJg+tSJ2fmjIJTkfuYUz574dFX6/VXxSxwGH/xQEAKS5TCsBK3CwnuG1p5SAsQq3gGVozDWyjEBcWDMdy8/AIFrj/y03Lfc/RNRCQTAfZbnf2QwV7sluw4fH3XJr07UoD0YqN+7XZzidtrwqMY26fpLZnyZjnBEt1FAZWO7RnKG5asg8xRk9YaDdedXdQSJAOy6bWEWlABj+tVAigBxavaluUH8LOj+yfCFldJjNLdi90fVHkUD/m4Mr5OtmupNMXPwuG3EQlqWUVpQoYpUYKLsk7a5Mvg6UFkiH596y5IbJEVCI1Kb3D1",
     "e3+wo77iKcILiZegnzyUNcjCdoQ="),
]


def reference(data):
    h = 0
    for j, b in enumerate(data):
        s = (j * 11) % 160
        v = b << s
        h ^= (v & ((1 << 160) - 1)) | (v >> 160)
    out = bytearray(h.to_bytes(20, "little"))
    for k, lb in enumerate(len(data).to_bytes(8, "little")):
        out[12 + k] ^= lb
    return base64.b64encode(bytes(out)).decode()


class TestQuickXor(unittest.TestCase):
    def hash_bytes(self, data):
        fd, p = tempfile.mkstemp()
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            return graph.quickxor_file(p)
        finally:
            os.remove(p)

    def test_rclone_vectors(self):
        for inp, want in VECTORS:
            data = base64.b64decode(inp)
            self.assertEqual(self.hash_bytes(data), want, len(data))
            self.assertEqual(reference(data), want, len(data))

    def test_chunk_boundaries_match_reference(self):
        rnd = random.Random(7)
        with mock.patch.object(graph, "QX_CHUNK", 480):
            for n in (159, 160, 161, 479, 480, 481, 1000, 4097):
                data = bytes(rnd.randrange(256) for _ in range(n))
                self.assertEqual(self.hash_bytes(data), reference(data), n)

    def test_remote_hash_prefers_sha1_then_quickxor(self):
        self.assertEqual(graph.remote_hash({"file": {"hashes": {"sha1Hash": "ab", "quickXorHash": "q"}}}), ("sha1", "AB"))
        self.assertEqual(graph.remote_hash({"file": {"hashes": {"quickXorHash": "q"}}}), ("quickxor", "q"))
        self.assertEqual(graph.remote_hash({"file": {}}), (None, None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
