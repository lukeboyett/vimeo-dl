import argparse
import hashlib
import json
import os
import tempfile
import unittest
from unittest import mock

import video


class NullBar:
    def __init__(self):
        self.total = 0

    def update(self, amount):
        self.total += amount


class Response:
    status_code = 200

    def __init__(self, body):
        self.body = body
        self.headers = {'Content-Length': str(len(body))}

    def iter_content(self, chunk_size):
        yield self.body


class VimeoDlTests(unittest.TestCase):
    def setUp(self):
        video._shutdown.clear()

    def test_successful_segment_is_atomic_hashed_and_resumable(self):
        body = b'complete media segment'
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'video_segment_0.tmp')
            progress = {'completed_segments': {}}
            bar = NullBar()
            fake_requests = mock.Mock()
            fake_requests.get.return_value = Response(body)
            fake_requests.exceptions.RequestException = OSError
            with mock.patch.object(video, 'requests', fake_requests):
                result = video.download_segment(
                    'https://example.test/0.m4s', path, 'video_0', len(body),
                    temp_dir, progress, bar, bar, 1,
                )

            self.assertEqual(result, ('video_0', True, 'downloaded'))
            self.assertFalse(os.path.exists(path + '.part'))
            self.assertEqual(progress['completed_segments']['video_0'], {
                'size': len(body), 'sha256': hashlib.sha256(body).hexdigest(),
            })
            self.assertTrue(video.is_segment_complete(path, progress, 'video_0', len(body)))
            with open(os.path.join(temp_dir, 'progress.json')) as source:
                self.assertEqual(json.load(source), {
                    'completed_segments': progress['completed_segments'],
                    'stream_fingerprints': {},
                })

    def test_off_by_one_advertised_size_uses_content_length(self):
        body = b'complete media segment'
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'video_segment_0.tmp')
            progress = {'completed_segments': {}}
            fake_requests = mock.Mock()
            fake_requests.get.return_value = Response(body)
            fake_requests.exceptions.RequestException = OSError
            with mock.patch.object(video, 'requests', fake_requests):
                result = video.download_segment(
                    'https://example.test/0.m4s', path, 'video_0', len(body) - 1,
                    temp_dir, progress, NullBar(), NullBar(), 1,
                )

            self.assertEqual(result, ('video_0', True, 'downloaded'))
            self.assertTrue(video.is_segment_complete(
                path, progress, 'video_0', len(body) - 1,
            ))

    def test_size_mismatch_fails_without_publishing_partial_segment(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'video_segment_0.tmp')
            progress = {'completed_segments': {}}
            fake_requests = mock.Mock()
            response = Response(b'short')
            response.headers['Content-Length'] = str(len(response.body) + 2)
            fake_requests.get.return_value = response
            fake_requests.exceptions.RequestException = OSError
            with mock.patch.object(video, 'requests', fake_requests):
                result = video.download_segment(
                    'https://example.test/0.m4s', path, 'video_0', 99,
                    temp_dir, progress, NullBar(), NullBar(), 1,
                )
            self.assertEqual(result, ('video_0', False, 'failed'))
            self.assertFalse(os.path.exists(path))
            self.assertFalse(os.path.exists(path + '.part'))

    def test_hash_detects_same_size_corruption(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = os.path.join(temp_dir, 'segment')
            with open(path, 'wb') as target:
                target.write(b'bad!')
            progress = {'completed_segments': {'video_0': {
                'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest(),
            }}}
            self.assertFalse(video.is_segment_complete(path, progress, 'video_0', 4))

    def test_master_downloader_failure_is_propagated(self):
        args = argparse.Namespace()
        with mock.patch.object(video.subprocess, 'run', return_value=argparse.Namespace(returncode=7)):
            self.assertFalse(video.download_single(
                'https://example.test/master.json?token=x', 'output.mp4', args,
                {'yt_dlp': True, 'youtube_dl': False, 'ffmpeg': False},
            ))

    def test_corrupt_manifest_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            with open(os.path.join(temp_dir, 'progress.json'), 'w') as target:
                target.write('{')
            with self.assertRaisesRegex(RuntimeError, 'use --clean'):
                video.load_progress(temp_dir)

    def test_pep668_environment_bootstraps_venv(self):
        distributions = [mock.Mock(metadata={'Name': 'requests'})]
        with mock.patch('importlib.metadata.distributions', return_value=distributions), \
                mock.patch.object(video, '_externally_managed', return_value=True), \
                mock.patch.object(video, '_bootstrap_venv') as bootstrap:
            video.ensure_deps()
        bootstrap.assert_called_once_with({'tqdm', 'moviepy'})

    def test_pep668_venv_failure_is_actionable(self):
        with mock.patch.object(video.venv.EnvBuilder, 'create', side_effect=OSError('denied')):
            with self.assertRaisesRegex(RuntimeError, 'Create a venv manually'):
                video._bootstrap_venv({'requests'})


if __name__ == '__main__':
    unittest.main()
