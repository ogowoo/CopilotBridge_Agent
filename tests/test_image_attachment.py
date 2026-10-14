# -*- coding: utf-8 -*-
"""粘贴图片附件：后端 data URL 解码落盘与上限。"""
import base64
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cbp import bridge as b


def data_url(content: bytes, ext: str = "png") -> str:
    return f"data:image/{ext};base64," + base64.b64encode(content).decode()


class ImageAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cbp_imgupload_"))
        self.saved = b.BASE
        b.BASE = self.tmp  # 让 uploads 落到临时目录，不污染真实 data/

    def tearDown(self):
        b.BASE = self.saved

    def test_decode_and_save(self):
        payload = b"\x89PNG\r\n\x1a\n" + b"fake-image-bytes" * 10
        paths = b.save_image_attachments([data_url(payload)])
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].is_file())
        self.assertEqual(paths[0].read_bytes(), payload)
        self.assertEqual(paths[0].suffix, ".png")

    def test_multiple_and_ext(self):
        paths = b.save_image_attachments([
            data_url(b"a" * 10, "png"),
            data_url(b"b" * 10, "jpeg"),
        ])
        self.assertEqual(len(paths), 2)
        self.assertEqual(paths[0].suffix, ".png")
        self.assertEqual(paths[1].suffix, ".jpg")

    def test_empty_and_invalid(self):
        self.assertEqual(b.save_image_attachments(None), [])
        self.assertEqual(b.save_image_attachments([]), [])
        self.assertEqual(b.save_image_attachments(["not-a-data-url"]), [])

    def test_size_and_count_caps(self):
        big = data_url(b"x" * (b.MAX_IMAGE_BYTES + 1))
        many = [data_url(b"y" * 10)] * (b.MAX_IMAGE_ATTACHMENTS + 2)
        self.assertEqual(b.save_image_attachments([big]), [])       # 超 8MB 被拒
        self.assertEqual(len(b.save_image_attachments(many)), b.MAX_IMAGE_ATTACHMENTS)  # 最多 4 张


if __name__ == "__main__":
    unittest.main()
