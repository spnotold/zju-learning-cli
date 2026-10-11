"""发布提醒的离线回归测试，不使用真实账号或通知记录。"""
import argparse
import datetime as dt
import importlib.util
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

import requests

spec = importlib.util.spec_from_file_location("zju_notify", Path(__file__).resolve().parent.parent / "zju.py")
zju = importlib.util.module_from_spec(spec)
spec.loader.exec_module(zju)

NOW = dt.datetime(2026, 10, 11, 10, tzinfo=dt.timezone.utc)
COURSE = {"id": 10, "name": "测试课程", "academic_year_id": 1}


def activity(**changes):
    return {"id": 1, "type": "homework", "title": "随堂练习", "published": True,
            "end_time": "2026-10-11T12:00:00Z", **changes}


def response(data, status=200):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps(data).encode()
    return r


class Notifications(unittest.TestCase):
    def setUp(self):
        self.seen = {}

    def poll(self, rows, now=NOW, minutes=30, course=COURSE):
        return zju.assessment_notifications(course, rows, self.seen, now, minutes)

    def test_new_and_dedup_include_type_and_course(self):
        rows = [activity(), activity(type="exam")]
        self.assertEqual([e["reasons"] for e in self.poll(rows)], [["new"], ["new"]])
        self.assertEqual(self.poll(rows), [])
        self.assertEqual(len(self.poll(rows, course={**COURSE, "id": 11})), 2)

    def test_scheduled_opening_and_reopening(self):
        row = activity(start_time="2026-10-11T10:05:00Z")
        self.assertEqual(self.poll([row])[0]["status"], "upcoming")
        self.assertEqual(self.poll([row]), [])
        later = NOW + dt.timedelta(minutes=6)
        self.assertEqual(self.poll([row], later)[0]["reasons"], ["opened"])
        self.assertEqual(self.poll([row], later), [])
        self.assertEqual(self.poll([{**row, "is_closed": True}], later), [])
        self.assertEqual(self.poll([row], later)[0]["reasons"], ["opened"])

    def test_server_start_flag_and_publication(self):
        row = activity(published=False)
        self.assertEqual(self.poll([row]), [])
        row.update(published=True, is_started=False)
        self.assertEqual(self.poll([row])[0]["reasons"], ["new"])
        row["is_started"] = True
        self.assertEqual(self.poll([row])[0]["reasons"], ["opened"])

    def test_skip_closed_expired_and_submitted(self):
        for changes in ({"is_closed": True}, {"end_time": NOW.isoformat()},
                        {"submitted": True}, {"is_submitted": True}):
            with self.subTest(changes=changes):
                self.assertEqual(self.poll([activity(**changes)]), [])

    def test_timed_homework_due_once_and_deadline_extension(self):
        row = activity(end_time=(NOW + dt.timedelta(minutes=10)).isoformat())
        self.assertEqual(self.poll([row])[0]["reasons"], ["new", "due"])
        self.assertEqual(self.poll([row]), [])
        row["end_time"] = (NOW + dt.timedelta(hours=1)).isoformat()
        self.assertEqual(self.poll([row])[0]["reasons"], ["schedule_changed"])
        later = NOW + dt.timedelta(minutes=30)
        self.assertEqual(self.poll([row], later)[0]["reasons"], ["due"])
        self.assertEqual(self.poll([row], later), [])

    def test_disable_due_and_time_zone_normalization(self):
        row = activity(start_time="2026-10-11 18:00:00", end_time="2026-10-11 18:15:00")
        self.assertEqual(self.poll([row], minutes=0)[0]["reasons"], ["new"])
        row["start_time"] = "2026-10-11T10:00:00Z"
        row["end_time"] = "2026-10-11T18:15:00+08:00"
        self.assertEqual(self.poll([row], minutes=0), [])
        self.assertEqual(self.poll([row])[0]["reasons"], ["due"])

    def test_unknown_and_invalid_times(self):
        event, = self.poll([activity(end_time=None)])
        self.assertEqual(event["reasons"], ["new"])
        self.assertIsNone(event["end_time"])
        with self.assertRaises(zju.ZjuError):
            self.poll([activity(end_time="invalid")])
        self.assertEqual(zju.assessment_snapshot(activity(end_time=None, deadline=NOW.isoformat()), NOW)["status"],
                         "closed")

    def test_api_pagination_and_source_types(self):
        z = zju.Zju.__new__(zju.Zju)
        z.logged_in = True
        replies = [response({"homework_activities": [activity()], "pages": 2}),
                   response({"homework_activities": [activity(id=2)], "pages": 2}),
                   response({"exams": [{"id": 1, "title": "测验"}]})]
        with mock.patch.object(z, "req", side_effect=replies) as req:
            rows = z.assessments(10, ["homework", "exam"])
        self.assertEqual([(r["id"], r["type"]) for r in rows], [(1, "homework"), (2, "homework"), (1, "exam")])
        self.assertEqual([c.kwargs["params"]["page"] for c in req.call_args_list], [1, 2, 1])
        self.assertTrue(all(c.args[0] == "GET" for c in req.call_args_list))

    def test_api_failure_is_not_an_empty_list(self):
        z = zju.Zju.__new__(zju.Zju)
        z.logged_in = True
        for reply in (response({"exams": []}, 403), response({"exams": []}, 500),
                      response({"error": "expired session"}), response({"exams": None}),
                      response({"exams": [{"title": "missing id"}]})):
            with self.subTest(reply=reply.content), mock.patch.object(z, "get", return_value=reply):
                with self.assertRaises(zju.ZjuError):
                    z.assessments(10, ["exam"])

    def test_empty_homework_page(self):
        z = zju.Zju.__new__(zju.Zju)
        z.logged_in = True
        with mock.patch.object(z, "get", return_value=response({"homework_activities": [], "pages": 0})):
            self.assertEqual(z.assessments(10, ["homework"]), [])

    def test_persistent_state_is_account_scoped_and_locked(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)):
            self.poll([activity()])
            with zju.notification_state(1) as seen:
                seen.update(self.seen)
                with self.assertRaises(zju.ZjuError):
                    with zju.notification_state(1):
                        self.fail("overlapping poll acquired the lock")
            with zju.notification_state(1) as seen:
                self.assertEqual(seen, self.seen)
            with zju.notification_state(2) as seen:
                self.assertEqual(seen, {})
            path = Path(d) / "notifications-1.json"
            before = path.read_bytes()
            with self.assertRaises(zju.ZjuError):
                with zju.notification_state(1) as seen:
                    seen.clear()
                    raise zju.ZjuError("second course failed")
            self.assertEqual(path.read_bytes(), before)

    def test_corrupt_state_is_preserved(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)):
            path = Path(d) / "notifications-1.json"
            for content in ('broken', '[]', '{"version":1,"items":{"bad":{}}}'):
                path.write_text(content)
                with self.assertRaises(zju.ZjuError):
                    with zju.notification_state(1):
                        self.fail("corrupt state was accepted")
                self.assertEqual(path.read_text(), content)

    def test_command_init_new_events_and_filters(self):
        z = mock.Mock()
        z.courses.return_value = [COURSE]
        z.user_id.return_value = 1
        rows = [activity(end_time=None)]
        z.assessments.side_effect = lambda cid, types: [r for r in rows if r["type"] in types]
        args = argparse.Namespace(course=[], all=False, type=["exam", "homework"], due_within=30,
                                  init=True, json=True, in_class=False, margin=10, outbox=None)

        def run():
            with redirect_stdout(io.StringIO()) as out:
                zju.cmd_notify(args)
            return json.loads(out.getvalue())

        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)), \
                mock.patch.object(zju, "Zju", return_value=z):
            self.assertEqual(run(), [])
            args.init = False
            self.assertEqual(run(), [])
            rows.append(activity(id=2, type="exam", end_time=None))
            args.type = ["homework"]
            self.assertEqual(run(), [])
            args.type = ["exam"]
            self.assertEqual(run()[0]["id"], 2)
            self.assertEqual(run(), [])
            args.type = ["homework", "exam"]
            self.assertEqual(run(), [])

    def test_cli_accepts_push_script_arguments_and_writes_outbox(self):
        # 从命令行入口验证定时推送脚本的参数，避免只测 Namespace 而漏掉 --outbox 未注册。
        for in_class in (False, True):
            with self.subTest(in_class=in_class), tempfile.TemporaryDirectory() as d:
                box = Path(d) / "outbox.json"
                argv = ["zju.py", "notify", "--json", "--due-within", "150", "--outbox", str(box)]
                if in_class:
                    argv.append("--in-class")
                z = mock.Mock()
                z.courses.return_value = [COURSE]
                z.user_id.return_value = 1
                z.assessments.return_value = [activity(end_time=None)]
                sub = {"course_name": COURSE["name"], "begin": 1000, "over": 2000}
                with mock.patch.object(zju.sys, "argv", argv), \
                        mock.patch.object(zju, "load_config", return_value={}), \
                        mock.patch.object(zju, "STATE_DIR", Path(d)), \
                        mock.patch.object(zju, "Zju", return_value=z), \
                        mock.patch.object(zju, "today_subs", return_value=[sub]), \
                        mock.patch.object(zju.time, "time", return_value=1500), \
                        mock.patch.object(zju, "log"), redirect_stdout(io.StringIO()) as out:
                    zju.main()
                events = json.loads(out.getvalue())
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["id"], 1)
                self.assertEqual(json.loads(box.read_text()), events)
                z.assessments.assert_called_once_with(COURSE["id"], ["exam", "homework"])

    def test_in_class_skips_api_and_narrows_to_current_course(self):
        z = mock.Mock()
        other = {**COURSE, "id": 11, "name": "别的课"}
        z.courses.return_value = [COURSE, other]
        z.user_id.return_value = 1
        z.assessments.return_value = []
        args = argparse.Namespace(course=[], all=False, type=["homework"], due_within=30, init=False, json=True,
                                  in_class=True, margin=10, outbox=None)
        sub = {"course_name": "测试课程", "begin": 1000, "over": 2000}
        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)), \
                mock.patch.object(zju, "Zju", return_value=z), mock.patch.object(zju, "today_subs", return_value=[sub]):
            with mock.patch("time.time", return_value=5000), redirect_stdout(io.StringIO()) as out:
                zju.cmd_notify(args)
            self.assertEqual(out.getvalue().strip(), "[]")
            z.courses.assert_not_called()
            z.assessments.assert_not_called()
            with mock.patch("time.time", return_value=1500), redirect_stdout(io.StringIO()):
                zju.cmd_notify(args)
            self.assertEqual([c.args[0] for c in z.assessments.call_args_list], [10])
            z.assessments.reset_mock()
            sub["course_name"] = "课表里的别名"  # 对不上时全查
            with mock.patch("time.time", return_value=1500), redirect_stdout(io.StringIO()):
                zju.cmd_notify(args)
            self.assertEqual([c.args[0] for c in z.assessments.call_args_list], [10, 11])
            z.assessments.reset_mock()
            alias = {"course_name": "课表里的别名", "begin": 1000, "over": 2000}
            sub["course_name"] = "测试课程"  # 两门课重叠、其中一门对不上：也全查
            with mock.patch.object(zju, "today_subs", return_value=[sub, alias]), \
                    mock.patch("time.time", return_value=1500), redirect_stdout(io.StringIO()):
                zju.cmd_notify(args)
            self.assertEqual([c.args[0] for c in z.assessments.call_args_list], [10, 11])

    def test_outbox_is_written_with_state_and_survives_broken_output(self):
        z = mock.Mock()
        z.courses.return_value = [COURSE]
        z.user_id.return_value = 1
        z.assessments.return_value = [activity(end_time=None)]
        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)), \
                mock.patch.object(zju, "Zju", return_value=z):
            box = Path(d) / "out.json"
            args = argparse.Namespace(course=[], all=False, type=["homework"], due_within=30, init=False,
                                      json=True, in_class=False, margin=10, outbox=str(box))
            box.write_text("broken")
            with redirect_stdout(io.StringIO()), self.assertRaises(zju.ZjuError):
                zju.cmd_notify(args)
            self.assertFalse((Path(d) / "notifications-1.json").exists())
            box.write_text('[{"id": 0}]')
            with redirect_stdout(io.StringIO()):
                zju.cmd_notify(args)
            self.assertEqual([e["id"] for e in json.loads(box.read_text())], [0, 1])
            with redirect_stdout(io.StringIO()):
                zju.cmd_notify(args)  # 没有新事件时不动队列
            self.assertEqual(len(json.loads(box.read_text())), 2)

    def test_outbox_append_waits_for_consumer_lock(self):
        import threading
        with tempfile.TemporaryDirectory() as d:
            box = Path(d) / "out.json"
            box.write_text("[1]")
            done = threading.Event()
            with zju.file_lock(box.with_name("out.json.lock")):
                t = threading.Thread(target=lambda: (zju.append_outbox(box, [2]), done.set()))
                t.start()
                self.assertFalse(done.wait(0.3))  # 推送程序持锁时生产者等待，不会覆盖
                box.write_text("[]")  # 推送程序在锁内删掉已发送的 1
            t.join(5)
            self.assertEqual(json.loads(box.read_text()), [2])

    def test_unopenable_lock_is_not_reported_as_busy(self):
        import errno
        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)):
            with zju.notification_state(1):
                with self.assertRaisesRegex(zju.ZjuError, "另一个 notify 正在运行"):
                    with zju.notification_state(1):
                        pass
            with mock.patch("os.open", side_effect=PermissionError(errno.EACCES, "denied")):
                with self.assertRaises(PermissionError):
                    with zju.notification_state(1):
                        pass

    def test_courses_later_page_error_is_not_partial_list(self):
        z = zju.Zju.__new__(zju.Zju)
        pages = [response({"courses": [COURSE], "pages": 2}), response({"error": "boom"}, 500)]
        with mock.patch.object(z, "ensure"), mock.patch.object(z, "post", side_effect=pages):
            with self.assertRaises(zju.ZjuError):
                z.courses()
        with mock.patch.object(z, "ensure"), mock.patch.object(z, "post", return_value=response({"courses": []})):
            self.assertEqual(z.courses(), [])

    def test_day_subs_error_is_not_an_empty_timetable(self):
        z = zju.Zju.__new__(zju.Zju)
        with mock.patch.object(z, "ensure"), mock.patch.object(z, "bearer", return_value={}):
            for r in (response({"error": "forbidden"}, 403), response({"code": 1, "msg": "x"}),
                      response({"code": 0})):
                with mock.patch.object(z, "get", return_value=r), self.assertRaises(zju.ZjuError):
                    z.day_subs(dt.date(2026, 10, 12))
            with mock.patch.object(z, "get", return_value=response({"code": 0, "list": []})):
                self.assertEqual(z.day_subs(dt.date(2026, 10, 12)), [])

    def test_command_failure_does_not_consume_any_events(self):
        z = mock.Mock()
        z.courses.return_value = [COURSE, {**COURSE, "id": 11}]
        z.user_id.return_value = 1
        args = argparse.Namespace(course=[], all=False, type=["homework"], due_within=30, init=False, json=True,
                                  in_class=False, margin=10, outbox=None)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)), \
                mock.patch.object(zju, "Zju", return_value=z):
            z.assessments.side_effect = [[activity(end_time=None)], zju.ZjuError("API failed")]
            with redirect_stdout(io.StringIO()) as out, self.assertRaises(zju.ZjuError):
                zju.cmd_notify(args)
            self.assertEqual(out.getvalue(), "")
            self.assertFalse((Path(d) / "notifications-1.json").exists())
            z.assessments.side_effect = [[activity(end_time=None)], []]
            with redirect_stdout(io.StringIO()) as out:
                zju.cmd_notify(args)
            self.assertEqual(json.loads(out.getvalue())[0]["reasons"], ["new"])

    def test_broken_output_does_not_consume_events(self):
        z = mock.Mock()
        z.courses.return_value = [COURSE]
        z.user_id.return_value = 1
        z.assessments.return_value = [activity(end_time=None)]
        args = argparse.Namespace(course=[], all=False, type=["homework"], due_within=30, init=False, json=True,
                                  in_class=False, margin=10, outbox=None)
        with tempfile.TemporaryDirectory() as d, mock.patch.object(zju, "STATE_DIR", Path(d)), \
                mock.patch.object(zju, "Zju", return_value=z), \
                mock.patch("sys.stdout", **{"write.side_effect": BrokenPipeError()}):
            with self.assertRaises(zju.ZjuError):
                zju.cmd_notify(args)
            self.assertFalse((Path(d) / "notifications-1.json").exists())


if __name__ == "__main__":
    unittest.main()
