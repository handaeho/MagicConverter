"""Shared IPC contracts for tray and headless hosts."""
from pathlib import Path

from magic_converter.core.automation.automation_store import recipe_from_dict
from magic_converter.core.conversion.builtin_converters import runtime_catalog
from magic_converter.core.conversion.conversion_request import prepare_request


class ApplicationApi:
    def __init__(self, service, preferences, history, *, watch=None, open_files=None, settings=None, stop=None):
        self.service, self.preferences, self.history = service, preferences, history
        self.watch, self.open_files, self.settings, self.stop = watch, open_files, settings, stop
        self.instance = ""
        self.closing = lambda: False

    def handle(self, request):
        if self.closing():
            raise ValueError("프로그램이 종료 중입니다.")
        command = request.get("command")
        if command == "status":
            return {"jobs": [{"job_id": job.job_id, "action": job.action.key, "total": len(job.files),
                               "files": [{"name": file.source.name, "state": file.state} for file in job.files[:64]]}
                              for job in self.service.job_snapshots()[:100]],
                    "watch_running": bool(self.watch and self.watch.running),
                    "recent": [{"id": item["id"], "action": item["action_key"], "files": len(item["files"])} for item in self.history.entries[-20:]]}
        if command in {"cancel", "stop"}:
            if request.get("instance") != self.instance:
                raise ValueError("앱이 재시작되었습니다. status의 새 인스턴스 ID를 사용하세요.")
            if command == "stop":
                if not self.stop:
                    raise ValueError("종료 명령을 지원하지 않습니다.")
                self.stop()
                return {"stopping": True}
            job_id = request.get("job_id")
            if job_id is None:
                if self.watch:
                    self.watch.stop()
                self.service.cancel_all()
                return {"cancelled": True}
            if type(job_id) is not int or job_id <= 0:
                raise ValueError("작업 ID가 올바르지 않습니다.")
            return {"cancelled": self.service.cancel_job(job_id)}
        if command == "settings":
            if not self.settings:
                raise ValueError("이 인스턴스에는 설정 창이 없습니다.")
            self.settings()
            return {}
        paths = request.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 256 or any(
                not isinstance(path, str) or len(path) > 4096 or not Path(path).is_absolute() for path in paths):
            raise ValueError("절대 파일 경로 1~256개를 지정하세요.")
        if command == "open":
            if not self.open_files:
                raise ValueError("이 인스턴스에는 변환 선택 창이 없습니다.")
            self.open_files(paths)
            return {"files": len(paths)}
        if command == "send":
            recipe = recipe_from_dict(request["recipe"])
            if recipe.action_key in {"copy_path", "terminal"}:
                raise ValueError("IPC에는 파일 변환 작업을 지정하세요.")
            preferences = self.preferences()
            planned = prepare_request(paths, recipe.action_key, runtime_catalog(self.service.ffmpeg), preset=recipe, output=preferences.output)
            return {"job_id": self.service.submit_request(planned)}
        raise ValueError("지원하지 않는 IPC 명령입니다.")
