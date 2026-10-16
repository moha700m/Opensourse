import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class UploadProtocolTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.output = patch.object(main, "OUTPUT_DIR", Path(self.directory.name))
        self.output.start()
        self.jobs = patch.dict(main._jobs, {}, clear=True)
        self.jobs.start()
        self.locks = patch.dict(main._upload_locks, {}, clear=True)
        self.locks.start()
        self.auth = patch.object(main, "_app_password", "")
        self.auth.start()
        self.client = TestClient(main.app)

    def tearDown(self):
        self.client.close()
        self.auth.stop()
        self.locks.stop()
        self.jobs.stop()
        self.output.stop()
        self.directory.cleanup()

    def create(self, size=12):
        response = self.client.post("/api/uploads", json={"filename": "phone.MOV", "size": size})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def test_resume_rejects_duplicate_chunk_and_queues_once(self):
        job_id = self.create()
        endpoint = f"/api/uploads/{job_id}"
        first = self.client.put(endpoint, params={"offset": 0}, content=b"abcde")
        self.assertEqual(first.json()["received_bytes"], 5)
        duplicate = self.client.put(endpoint, params={"offset": 0}, content=b"abcde")
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(duplicate.json()["detail"]["received_bytes"], 5)
        self.assertEqual(self.client.post(endpoint + "/complete").status_code, 409)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").json()["received_bytes"], 5)
        rest = self.client.put(endpoint, params={"offset": 5}, content=b"fghijkl")
        self.assertEqual(rest.json()["received_bytes"], 12)
        with patch.object(main, "_run_job") as worker:
            self.assertEqual(self.client.post(endpoint + "/complete").status_code, 202)
            self.assertEqual(self.client.post(endpoint + "/complete").status_code, 202)
            self.assertEqual(worker.call_count, 1)
        self.assertEqual((Path(self.directory.name) / job_id / "upload.mov").read_bytes(), b"abcdefghijkl")
        public = self.client.get(f"/api/jobs/{job_id}").json()
        self.assertNotIn("source_path", public)
        self.assertNotIn("options", public)
        self.assertEqual(self.client.delete(f"/api/jobs/{job_id}").status_code, 409)

    def test_rejected_chunks_never_modify_committed_bytes(self):
        job_id = self.create()
        endpoint = f"/api/uploads/{job_id}"
        with patch.object(main, "UPLOAD_CHUNK_BYTES", 4):
            self.assertEqual(self.client.put(endpoint, content=b"12345").status_code, 413)
        self.assertEqual(self.client.put(endpoint, content=b"").status_code, 422)
        self.assertEqual(self.client.put(endpoint, content=b"x" * 13).status_code, 422)
        self.assertEqual((Path(self.directory.name) / job_id / "upload.part").stat().st_size, 0)

    def test_invalid_file_and_options_are_rejected_before_creating_state(self):
        for payload in [
            {"filename": "source.txt", "size": 20},
            {"filename": "source.mp4", "size": 0},
            {"filename": "source.mp4", "size": main.MAX_UPLOAD_BYTES + 1},
            {"filename": "source.mp4", "size": 20, "min_seconds": 60, "max_seconds": 30},
        ]:
            self.assertEqual(self.client.post("/api/uploads", json=payload).status_code, 422)
        self.assertEqual(list(Path(self.directory.name).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
