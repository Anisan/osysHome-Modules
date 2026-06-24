import requests
import re
import zipfile
import os
import sys
import time
import datetime
import shutil
import subprocess
import base64
import threading
import uuid
from html import escape
from urllib.parse import urlparse
from app.configuration import Config
from flask import render_template, redirect, jsonify, request, current_app
from sqlalchemy import text
from app.core.main.BasePlugin import BasePlugin
from app.core.main.PluginsHelper import plugins
from app.core.models.Plugins import Plugin
from app.authentication.handlers import handle_admin_required
from app.database import (
    db,
    session_scope,
    get_now_to_utc,
    convert_utc_to_local,
    convert_local_to_utc,
    row2dict,
)
from app.core.lib.common import addNotify, CategoryNotify
from app.core.lib.object import setProperty, getProperty
from plugins.Modules.forms.SettingForms import SettingsForm

_markdown_lib = None
_markdown_module = None
try:
    import markdown
    _markdown_lib = 'markdown'
    _markdown_module = markdown
except ImportError:
    try:
        import markdown2
        _markdown_lib = 'markdown2'
        _markdown_module = markdown2
    except ImportError:
        pass

_operation_lock = threading.Lock()
_operation_jobs = {}
_github_user_status_lock = threading.Lock()
_github_user_status = {
    'message': None,
    'kind': None,
    'waiting_seconds': 0,
}

PROTECTED_MODULES = frozenset(['Modules', 'Objects', 'Users', 'Scheduler', 'wsServer'])

OPERATION_STEP_IDS = {
    'install': ['fetch_info', 'download', 'extract', 'install_files', 'cleanup', 'dependencies', 'register'],
    'upgrade': ['fetch_info', 'download', 'extract', 'install_files', 'cleanup', 'dependencies', 'update_db'],
    'upgrade_core': ['fetch_info', 'download', 'extract', 'install_files', 'cleanup', 'dependencies', 'update_db'],
    'uninstall': ['drop_tables', 'remove_files', 'unregister'],
}

class Modules(BasePlugin):

    def __init__(self,app):
        super().__init__(app,__name__)
        self.title = "Modules"
        self.description = """List modules"""
        self.category = "System"
        self.actions = ["search","cycle","widget"]
        self.author = "Eraser"

    def initialization(self):
        pass

    def route_github_api(self):
        base = f"/api/{self.name}/github"

        @self.blueprint.route(f"{base}/catalog", methods=["GET"])
        @handle_admin_required
        def github_catalog():
            with session_scope() as session:
                ps = session.query(Plugin).order_by(Plugin.name).all()
                installed = [row2dict(plugin) for plugin in ps]
            catalog = self.get_github_catalog(installed)
            return jsonify({"success": True, **catalog})

        @self.blueprint.route(f"{base}/status", methods=["GET"])
        @handle_admin_required
        def github_status():
            return jsonify({"success": True, **self._get_github_user_status()})

        @self.blueprint.route(f"{base}/branches", methods=["GET"])
        @handle_admin_required
        def github_branches():
            owner = request.args.get("owner")
            repo = request.args.get("repo")
            url = request.args.get("url")
            module_name = request.args.get("module")
            owner, repo = self._resolve_github_target(owner, repo, url=url, module_name=module_name)
            if not owner or not repo:
                return jsonify({"success": False, "message": "owner and repo are required"}), 400

            branches, error = self.get_github_branches(owner, repo)
            if error:
                return jsonify({"success": False, "message": error}), 502
            return jsonify({"success": True, "result": branches})

        @self.blueprint.route(f"{base}/commits", methods=["GET"])
        @handle_admin_required
        def github_commits():
            owner = request.args.get("owner")
            repo = request.args.get("repo")
            branch = request.args.get("branch")
            url = request.args.get("url")
            module_name = request.args.get("module")
            per_page = request.args.get("per_page", 15, type=int)
            owner, repo = self._resolve_github_target(owner, repo, url=url, module_name=module_name)
            if not owner or not repo:
                return jsonify({"success": False, "message": "owner and repo are required"}), 400

            commits, error = self.get_github_commits(owner, repo, branch=branch, per_page=per_page)
            if error:
                return jsonify({"success": False, "message": error}), 502
            return jsonify({"success": True, "result": commits})

    def route_readme(self):
        @self.blueprint.route(f"/api/{self.name}/readme", methods=["GET"])
        @handle_admin_required
        def module_readme():
            lang = request.args.get('lang', current_app.config.get('DEFAULT_LANGUAGE', 'en'))
            installed = request.args.get('installed', '0').lower() in ('1', 'true', 'yes')
            module_name = request.args.get('module')
            owner = request.args.get('owner')
            repo = request.args.get('repo')
            branch = request.args.get('branch')
            url = request.args.get('url')

            content, meta, error = self.get_readme_content(
                installed=installed,
                module_name=module_name,
                owner=owner,
                repo=repo,
                branch=branch,
                url=url,
                lang=lang,
            )
            if error:
                status = 404 if 'not found' in error.lower() else 502
                return jsonify({"success": False, "message": error}), status

            html_content, _ = self.markdown_to_html(
                content,
                lang=lang,
                image_prefix=meta.get('image_prefix'),
                github_raw_base=meta.get('github_raw_base'),
            )
            return jsonify({
                "success": True,
                "content": html_content,
                "lang": lang,
                "source": meta.get('source'),
            })

    def route_operations(self):
        base = f"/api/{self.name}/operations"

        @self.blueprint.route(base, methods=["POST"])
        @handle_admin_required
        def start_operation():
            data = request.get_json(silent=True) or {}
            op = (data.get('op') or '').strip()
            if op not in OPERATION_STEP_IDS:
                return jsonify({"success": False, "message": "Unknown operation"}), 400

            job_id = str(uuid.uuid4())
            steps = self._init_operation_steps(op)
            with _operation_lock:
                _operation_jobs[job_id] = {
                    'done': False,
                    'success': False,
                    'error': None,
                    'steps': steps,
                    'op': op,
                    'title': data.get('title') or '',
                }

            thread = threading.Thread(
                target=self._run_operation_job,
                args=(job_id, op, data),
                daemon=True,
            )
            thread.start()
            return jsonify({"success": True, "job_id": job_id})

        @self.blueprint.route(f"{base}/<job_id>", methods=["GET"])
        @handle_admin_required
        def operation_status(job_id):
            with _operation_lock:
                job = _operation_jobs.get(job_id)
                if not job:
                    return jsonify({"success": False, "message": "Job not found"}), 404
                return jsonify({
                    "success": True,
                    "done": job["done"],
                    "operation_success": job["success"],
                    "error": job.get("error"),
                    "steps": job["steps"],
                    "op": job.get("op"),
                    "title": job.get("title"),
                    "github_status": self._get_github_user_status(),
                })

    def _init_operation_steps(self, op):
        return [{"id": step_id, "status": "pending", "message": None} for step_id in OPERATION_STEP_IDS[op]]

    def _set_operation_step(self, job_id, step_id, status, message=None):
        with _operation_lock:
            job = _operation_jobs.get(job_id)
            if not job:
                return
            for step in job['steps']:
                if step['id'] == step_id:
                    step['status'] = status
                    if message is not None:
                        step['message'] = message
                    elif status != 'running':
                        step['message'] = None
                    break

    def _set_running_operation_step_message(self, job_id, message):
        with _operation_lock:
            job = _operation_jobs.get(job_id)
            if not job:
                return
            for step in job['steps']:
                if step['status'] == 'running':
                    step['message'] = message
                    break

    def _get_github_user_status(self):
        with _github_user_status_lock:
            return dict(_github_user_status)

    def _set_github_user_status(self, message=None, kind=None, waiting_seconds=0):
        with _github_user_status_lock:
            _github_user_status['message'] = message
            _github_user_status['kind'] = kind
            _github_user_status['waiting_seconds'] = int(waiting_seconds or 0)
        if message:
            job_id = getattr(self, '_github_status_job_id', None)
            if job_id:
                self._set_running_operation_step_message(job_id, message)

    def _format_rate_limit_wait_message(self, seconds):
        seconds = max(int(seconds), 1)
        return (
            f'GitHub API rate limit exceeded. '
            f'Waiting {seconds} seconds before retry. '
            f'Add a GitHub token in Modules settings to increase the limit.'
        )

    def _finish_operation_job(self, job_id, success, error=None):
        with _operation_lock:
            job = _operation_jobs.get(job_id)
            if not job:
                return
            job['done'] = True
            job['success'] = success
            job['error'] = error

    def _operation_step_callback(self, job_id):
        def callback(step_id, status, message=None):
            self._set_operation_step(job_id, step_id, status, message)
        return callback

    def _register_installed_module(self, name, owner, repo, updated):
        """Register or update a plugin row after files are installed."""
        url = f'https://github.com/{owner}/{repo}'
        module = Plugin.query.filter(Plugin.name == name).one_or_none()
        if module is None:
            module = Plugin()
            module.name = name
        module.updated = updated
        module.url = url
        module.save()
        db.session.commit()

    def _run_operation_job(self, job_id, op, data):
        with self._app.app_context():
            self._github_status_job_id = job_id
            try:
                if op == 'install':
                    self._execute_install(job_id, data)
                elif op == 'upgrade':
                    self._execute_upgrade(job_id, data, core=False)
                elif op == 'upgrade_core':
                    self._execute_upgrade(job_id, data, core=True)
                elif op == 'uninstall':
                    self._execute_uninstall(job_id, data)
                else:
                    raise ValueError(f"Unknown operation: {op}")
                self._finish_operation_job(job_id, True)
            except Exception as ex:
                self.logger.exception(ex)
                self._finish_operation_job(job_id, False, str(ex))
            finally:
                self._github_status_job_id = None
                if not self._get_github_user_status().get('message'):
                    self._set_github_user_status()

    def _execute_install(self, job_id, data):
        name = (data.get('name') or '').strip()
        owner = (data.get('author') or data.get('owner') or '').strip()
        if not name or not owner:
            raise ValueError("name and author are required")

        repo = f'osysHome-{name}'
        target_folder = os.path.join(Config.PLUGINS_FOLDER, name)
        step = self._operation_step_callback(job_id)

        self._set_operation_step(job_id, 'fetch_info', 'running')
        info = self.get_github_repo_info(owner, repo)
        if info is None:
            message = self._last_github_error_message("Failed to get repository info")
            self._set_operation_step(job_id, 'fetch_info', 'error', message)
            raise Exception(message)
        branch = info.get('default_branch') or 'master'
        self._set_operation_step(job_id, 'fetch_info', 'done')

        dt = self.download_and_extract_github_repo(
            owner, repo, branch, None, target_folder, step_callback=step,
        )

        self._set_operation_step(job_id, 'register', 'running')
        self._register_installed_module(name, owner, repo, dt)
        self._set_operation_step(job_id, 'register', 'done')

        addNotify("Success install", f'Success install module {name}', CategoryNotify.Info, self.name)
        setProperty("SystemVar.NeedRestart", True, self.name)

    def _execute_upgrade(self, job_id, data, core=False):
        commit = (data.get('commit') or '').strip() or None
        step = self._operation_step_callback(job_id)
        module = None

        if core:
            owner = 'Anisan'
            repo = 'osysHome'
            branch = getProperty("SystemVar.core_branch") or 'master'
            target_folder = os.path.join(Config.APP_DIR)
            module_name = 'osysHome'
        else:
            name = (data.get('name') or '').strip()
            url = (data.get('url') or '').strip()
            if not name:
                raise ValueError("name is required")
            module = Plugin.query.filter(Plugin.name == name).one_or_404()
            if module and module.url:
                url = module.url
            owner, repo = self.extract_owner_and_repo(url)
            if not owner or not repo:
                raise ValueError(f"Invalid repository URL for {name}")
            target_folder = os.path.join(Config.PLUGINS_FOLDER, name)
            module_name = name
            branch = module.branch

        if not commit:
            self._set_operation_step(job_id, 'fetch_info', 'running')
            info = self.get_github_repo_info(owner, repo)
            if info is None:
                message = self._last_github_error_message(f"Failed to get info for {owner}/{repo}")
                self._set_operation_step(job_id, 'fetch_info', 'error', message)
                raise Exception(message)
            if not branch:
                branch = info.get('default_branch') or 'master'
            self._set_operation_step(job_id, 'fetch_info', 'done')
        elif not branch:
            branch = 'master'

        dt = self.download_and_extract_github_repo(
            owner, repo, branch, commit, target_folder, step_callback=step,
        )

        self._set_operation_step(job_id, 'update_db', 'running')
        if core:
            dt = convert_utc_to_local(dt)
            setProperty("SystemVar.upgraded", dt, self.name)
            setProperty("SystemVar.update", False, self.name)
            addNotify("Success update", 'Success update osysHome', CategoryNotify.Info, self.name)
        else:
            module.updated = dt
            module.update = False
            db.session.commit()
            addNotify("Success update", f'Success update module {module_name}', CategoryNotify.Info, self.name)
        self._set_operation_step(job_id, 'update_db', 'done')
        setProperty("SystemVar.NeedRestart", True, self.name)

    def _execute_uninstall(self, job_id, data):
        name = (data.get('name') or '').strip()
        if not name:
            raise ValueError("name is required")
        if name in PROTECTED_MODULES:
            message = f'Cannot uninstall module {name}'
            self._set_operation_step(job_id, 'drop_tables', 'error', message)
            raise Exception(message)

        self._set_operation_step(job_id, 'drop_tables', 'running')
        with session_scope() as session:
            try:
                for _, model in db.Model.registry._class_registry.items():
                    if hasattr(model, '__tablename__') and hasattr(model, '__table__'):
                        table_name = model.__tablename__
                        module_name = model.__module__.split(".")[1]
                        if module_name == name:
                            self.logger.info(f"Drop table {table_name}")
                            query = text(f"DROP TABLE {table_name};")
                            session.execute(query)
                self._set_operation_step(job_id, 'drop_tables', 'done')

                self._set_operation_step(job_id, 'remove_files', 'running')
                folder_module = os.path.join(Config.PLUGINS_FOLDER, name)
                if os.path.exists(folder_module):
                    shutil.rmtree(folder_module)
                    self.logger.info(f"Deleted folder '{folder_module}'.")
                self._set_operation_step(job_id, 'remove_files', 'done')

                self._set_operation_step(job_id, 'unregister', 'running')
                module = session.query(Plugin).filter(Plugin.name == name).one_or_none()
                if module:
                    session.delete(module)
                db.session.commit()
                self._set_operation_step(job_id, 'unregister', 'done')
                addNotify("Success uninstall", f'Success uninstall module {name}', CategoryNotify.Info, self.name)
                setProperty("SystemVar.NeedRestart", True, self.name)
                self.logger.info(f"Unstalled module '{name}'")
            except Exception as ex:
                session.rollback()
                addNotify("Error install", f'Error uninstall module {name}', CategoryNotify.Error, self.name)
                raise

    def admin(self, request):

        op = request.args.get("op",None)
        is_ajax = request.args.get("ajax") == "1"

        if op == 'upgrade':
            name = request.args.get('name',None)
            url = request.args.get('url',None)
            commit = request.args.get('commit',None)
            module = Plugin.query.filter(Plugin.name == name).one_or_404()
            if module and module.url:
                url = module.url

            owner, repo = self.extract_owner_and_repo(url)
            try:
                info = self.get_github_repo_info(owner, repo)
                if info is None:
                    raise Exception(f"Failed to get info for {owner}/{repo}")
                branch = module.branch
                if branch is None or branch == '':
                    branch = info['default_branch']
                dt = self.download_and_extract_github_repo(owner, repo, branch, commit, os.path.join(Config.PLUGINS_FOLDER,name))
                module.updated = dt
                module.update = False
                db.session.commit()
                addNotify("Success update",f'Success update module {name}',CategoryNotify.Info,self.name)
                setProperty("SystemVar.NeedRestart", True, self.name)
                if is_ajax:
                    return jsonify({"status": "ok", "message": f"Success update module {name}"}), 200
            except Exception as ex:
                self.logger.exception(ex)
                addNotify("Error update",f'Error update module {name}',CategoryNotify.Error,self.name)
                if is_ajax:
                    return jsonify({"status": "error", "message": str(ex)}), 500

            if not is_ajax:
                return redirect(self.name)
            return jsonify({"status": "ok"}), 200

        if op == 'upgrade_core':
            owner = 'Anisan'
            repo = 'osysHome'
            commit = request.args.get('commit',None)
            branch = getProperty("SystemVar.core_branch")
            if branch is None:
                branch = 'master'
            try:
                dt = self.download_and_extract_github_repo(owner, repo, branch, commit, os.path.join(Config.APP_DIR))
                dt = convert_utc_to_local(dt)
                setProperty("SystemVar.upgraded", dt, self.name)
                setProperty("SystemVar.update", False, self.name)
                addNotify("Success update", 'Success update osysHome',CategoryNotify.Info,self.name)
                setProperty("SystemVar.NeedRestart", True, self.name)
                if is_ajax:
                    return jsonify({"status": "ok", "message": "Success update osysHome"}), 200
            except Exception as ex:
                self.logger.exception(ex)
                addNotify("Error update", 'Error update osysHome',CategoryNotify.Error,self.name)
                if is_ajax:
                    return jsonify({"status": "error", "message": str(ex)}), 500

            if not is_ajax:
                return redirect(self.name)
            return jsonify({"status": "ok"}), 200

        if op == 'install':
            name = request.args.get('name',None)
            owner = request.args.get('author',None)
            repo = 'osysHome-' + name

            try:
                info = self.get_github_repo_info(owner, repo)
                if info is None:
                    raise Exception(f"Failed to get info for {owner}/{repo}")
                branch = info['default_branch']
                dt = self.download_and_extract_github_repo(owner, repo, branch, None, os.path.join(Config.PLUGINS_FOLDER,name))
                self._register_installed_module(name, owner, repo, dt)
                addNotify("Success install",f'Success install module {name}',CategoryNotify.Info,self.name)
                setProperty("SystemVar.NeedRestart", True, self.name)
            except Exception as ex:
                self.logger.exception(ex)
                addNotify("Error install",f'Error install module {name}',CategoryNotify.Error,self.name)

            return redirect(self.name)

        if op == 'uninstall':
            name = request.args.get('name',None)
            self.logger.info(f"Unstalling module '{name}'")

            if name in PROTECTED_MODULES:
                addNotify("Error uninstall",f'Cannot uninstall module {name}',CategoryNotify.Error,self.name)
                return redirect(self.name)

            with session_scope() as session:
                try:
                    for _, model in db.Model.registry._class_registry.items():
                        if hasattr(model, '__tablename__') and hasattr(model, '__table__'):
                            table_name = model.__tablename__
                            module_name = model.__module__.split(".")[1]
                            if module_name == name:
                                self.logger.info(f"Drop table {table_name}")
                                query = text(f"DROP TABLE {table_name};")
                                session.execute(query)

                    folder_module = os.path.join(Config.PLUGINS_FOLDER,name)

                    if os.path.exists(folder_module):
                        shutil.rmtree(folder_module)
                        self.logger.info(f"Deleted folder '{folder_module}'.")

                    module = session.query(Plugin).filter(Plugin.name == name).one_or_none()
                    if module:
                        session.delete(module)
                    db.session.commit()
                    addNotify("Success uninstall",f'Success uninstall module {name}',CategoryNotify.Info,self.name)
                    setProperty("SystemVar.NeedRestart", True, self.name)
                    self.logger.info(f"Unstalled module '{name}'")

                except Exception as ex:
                    session.rollback()
                    self.logger.exception(ex)
                    addNotify("Error install",f'Error uninstall module {name}',CategoryNotify.Error,self.name)

            return redirect(self.name)

        settings = SettingsForm()
        if request.method == 'GET':
            settings.update_time.data = self.config.get('update_time',60)
            settings.token.data = self.config.get('token','')
        else:
            if settings.validate_on_submit():
                self.config["update_time"] = settings.update_time.data
                self.config["token"] = settings.token.data
                self.saveConfig()
                return redirect("Modules")

        content = {
            "form": settings,
        }
        return self.render("modules.html", content)

    def extract_owner_and_repo(self, url):
        pattern = r"https://github\.com/([^/]+)/([^/]+)"
        match = re.match(pattern, url)

        if match:
            owner, repo = match.groups()
            if repo.endswith('.git'):
                repo = repo[:-4]
            return owner, repo
        else:
            return None, None

    def _resolve_github_target(self, owner=None, repo=None, url=None, module_name=None):
        owner = (owner or '').strip() or None
        repo = (repo or '').strip() or None

        if owner and repo:
            return owner, repo

        if url:
            parsed_owner, parsed_repo = self.extract_owner_and_repo(url)
            if parsed_owner and parsed_repo:
                return parsed_owner, parsed_repo

        if module_name:
            name = module_name.strip()
            if name.lower() == 'osyshome':
                return 'Anisan', 'osysHome'
            if name.startswith('osysHome-'):
                return 'Anisan', name
            return 'Anisan', f'osysHome-{name}'

        return None, None

    def _github_auth_header(self, token):
        token = (token or '').strip()
        if not token:
            return None
        scheme = 'Bearer' if token.startswith('github_pat_') else 'token'
        return f'{scheme} {token}'

    def _github_base_headers(self):
        return {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'osysHome-Modules',
        }

    def _github_response_error(self, response):
        try:
            payload = response.json()
            if isinstance(payload, dict) and payload.get('message'):
                return payload['message']
        except ValueError:
            pass
        if response.text:
            return response.text[:200]
        return f'HTTP {response.status_code}'

    def _is_github_rate_limit(self, status_code, message):
        if status_code != 403:
            return False
        lower = (message or '').lower()
        return 'rate limit' in lower

    def _friendly_github_error(self, status_code, message):
        message = message or 'GitHub API unavailable'
        if self._is_github_rate_limit(status_code, message):
            token = (self.config.get('token') or '').strip()
            if not token:
                return (
                    'GitHub API rate limit exceeded. '
                    'Add a GitHub token in Modules settings to increase the limit.'
                )
            return (
                f'GitHub API rate limit exceeded: {message}. '
                'Wait a few minutes or check the token in Modules settings.'
            )
        return message

    def _last_github_error_message(self, default="GitHub API unavailable"):
        stored = getattr(self, '_last_github_error', None)
        if stored:
            return stored
        return default

    def _wait_interruptible(self, timeout):
        """Wait up to *timeout* seconds; return True if stop was requested."""
        event = self.event
        if event is not None:
            return event.wait(timeout)
        time.sleep(timeout)
        return False

    def stop_cycle(self):
        """Stop cycle with timeout to avoid blocking on GitHub rate-limit waits."""
        self.logger.info("Stopping cycle...")
        if self.event:
            self.event.set()
        if self.thread:
            self.thread.join(timeout=5.0)
            if self.thread.is_alive():
                self.logger.warning("Cycle thread did not stop within timeout")
            self.thread = None
        self.logger.info("Stopped cycle")

    def _github_rate_limit_wait(self, response):
        reset_time = response.headers.get('X-RateLimit-Reset')
        if reset_time:
            try:
                wait_time = int(reset_time) - time.time() + 5
            except (TypeError, ValueError):
                wait_time = 60
        else:
            wait_time = 60
        wait_time = min(max(wait_time, 0), 300)
        if wait_time <= 0:
            return False

        remaining = int(wait_time)
        self.logger.warning("GitHub rate limit hit, waiting %s seconds", remaining)
        while remaining > 0:
            message = self._format_rate_limit_wait_message(remaining)
            self._set_github_user_status(
                message,
                kind='rate_limit_wait',
                waiting_seconds=remaining,
            )
            sleep_chunk = min(1, remaining)
            if self._wait_interruptible(sleep_chunk):
                return False
            remaining -= sleep_chunk
        return True

    def github_request(self, url, allow_anonymous_fallback=True, rate_limit_retries=2):
        headers = self._github_base_headers()
        token = self.config.get('token', None)
        auth = self._github_auth_header(token)
        if auth:
            headers['Authorization'] = auth

        for attempt in range(rate_limit_retries + 1):
            try:
                response = requests.get(url, headers=headers, timeout=Config.HTTP_REQUEST_TIMEOUT)
            except requests.exceptions.RequestException as ex:
                self.logger.warning("GitHub API request failed for %s: %s", url, ex)
                self._last_github_error = str(ex)
                return {'_error': True, 'status': 0, 'message': str(ex)}

            if response.status_code == 200:
                self._last_github_error = None
                self._set_github_user_status()
                return response.json()

            if self._is_github_rate_limit(response.status_code, self._github_response_error(response)):
                if attempt < rate_limit_retries and self._github_rate_limit_wait(response):
                    continue

            if allow_anonymous_fallback and auth and response.status_code in (401, 403):
                if self._is_github_rate_limit(response.status_code, self._github_response_error(response)):
                    pass
                else:
                    self.logger.warning(
                        "GitHub token rejected (%s), retrying without authentication: %s",
                        response.status_code,
                        self._github_response_error(response),
                    )
                    try:
                        response = requests.get(
                            url,
                            headers=self._github_base_headers(),
                            timeout=Config.HTTP_REQUEST_TIMEOUT,
                        )
                    except requests.exceptions.RequestException as ex:
                        self.logger.warning("GitHub API anonymous retry failed for %s: %s", url, ex)
                        self._last_github_error = str(ex)
                        return {'_error': True, 'status': 0, 'message': str(ex)}
                    if response.status_code == 200:
                        self._last_github_error = None
                        self._set_github_user_status()
                        return response.json()

            message = self._friendly_github_error(response.status_code, self._github_response_error(response))
            self._last_github_error = message
            if self._is_github_rate_limit(response.status_code, message):
                self._set_github_user_status(message, kind='rate_limit_error')
            self.logger.warning("GitHub API error %s for %s: %s", response.status_code, url, message)
            return {'_error': True, 'status': response.status_code, 'message': message}

        message = self._last_github_error_message()
        return {'_error': True, 'status': 403, 'message': message}

    def _github_request_failed(self, data):
        if data is None:
            return True
        return isinstance(data, dict) and data.get('_error')

    def _github_error_message(self, data, default="GitHub API unavailable"):
        if isinstance(data, dict):
            return data.get('message') or default
        return default

    def get_github_branches(self, owner, repo):
        url = f"https://api.github.com/repos/{owner}/{repo}/branches"
        data = self.github_request(url)
        if self._github_request_failed(data):
            return None, self._github_error_message(data)
        return data, None

    def get_github_commits(self, owner, repo, branch=None, per_page=15):
        url = f"https://api.github.com/repos/{owner}/{repo}/commits?"
        if branch:
            url += f"sha={branch}&"
        url += f"per_page={per_page}"
        data = self.github_request(url)
        if self._github_request_failed(data):
            return None, self._github_error_message(data)
        if not isinstance(data, list):
            data = [data]
        return data, None

    def _readme_filenames(self, lang):
        filenames = []
        if lang and lang != 'en':
            filenames.append(f'README.{lang}.md')
        filenames.append('README.md')
        return filenames

    def _read_local_readme(self, base_path, lang):
        for filename in self._readme_filenames(lang):
            readme_path = os.path.join(base_path, filename)
            if os.path.isfile(readme_path):
                with open(readme_path, 'r', encoding='utf-8') as f:
                    return f.read(), filename
        return None, None

    def _fetch_github_readme(self, owner, repo, branch, lang):
        ref = branch or 'master'
        for filename in self._readme_filenames(lang):
            url = f"https://api.github.com/repos/{owner}/{repo}/contents/{filename}?ref={ref}"
            data = self.github_request(url)
            if self._github_request_failed(data):
                continue
            if isinstance(data, dict) and data.get('encoding') == 'base64' and data.get('content'):
                try:
                    return base64.b64decode(data['content']).decode('utf-8'), filename
                except (ValueError, UnicodeDecodeError) as ex:
                    self.logger.warning("Failed to decode README %s/%s: %s", owner, repo, ex)
        return None, None

    def markdown_to_html(self, readme_content, lang=None, image_prefix=None, github_raw_base=None):
        html_content = readme_content
        if _markdown_lib and _markdown_module:
            try:
                if _markdown_lib == 'markdown':
                    md = _markdown_module.Markdown(extensions=['fenced_code', 'tables', 'toc', 'codehilite'])
                    html_content = md.convert(readme_content)
                elif _markdown_lib == 'markdown2':
                    html_content = _markdown_module.markdown(
                        readme_content,
                        extras=['fenced-code-blocks', 'tables', 'header-ids'],
                    )

                def fix_image_path(match):
                    img_tag = match.group(0)
                    src_match = re.search(r'src=["\']([^"\']+)["\']', img_tag)
                    if not src_match:
                        return img_tag
                    src_path = src_match.group(1)
                    parsed = urlparse(src_path)
                    if parsed.scheme or src_path.startswith('/') or src_path.startswith('data:'):
                        return img_tag
                    clean_path = src_path.lstrip('./')
                    if github_raw_base:
                        absolute_path = f"{github_raw_base}{clean_path}"
                    elif image_prefix:
                        absolute_path = f"/{image_prefix}/{clean_path}"
                    else:
                        absolute_path = f"/{clean_path}"
                    return re.sub(r'src=["\'][^"\']+["\']', f'src="{absolute_path}"', img_tag)

                html_content = re.sub(r'<img[^>]+src=["\'][^"\']+["\'][^>]*>', fix_image_path, html_content)
            except Exception as ex:
                self.logger.warning("Error parsing Markdown: %s", ex)
                html_content = f"<pre>{escape(readme_content)}</pre>"
        else:
            html_content = f"<pre>{escape(readme_content)}</pre>"
        return html_content, image_prefix

    def get_readme_content(self, installed, module_name, owner=None, repo=None, branch=None, url=None, lang='en'):
        if installed:
            if not module_name:
                return None, None, "module is required"
            if module_name.lower() == 'osyshome':
                base_path = Config.APP_DIR
                meta = {'source': 'local', 'image_prefix': None}
            else:
                base_path = os.path.join(Config.PLUGINS_FOLDER, module_name)
                meta = {'source': 'local', 'image_prefix': module_name}
            content, _ = self._read_local_readme(base_path, lang)
            if content is None:
                return None, None, f"README file not found for '{module_name}'"
            return content, meta, None

        owner, repo = self._resolve_github_target(owner, repo, url=url, module_name=module_name)
        if not owner or not repo:
            return None, None, "owner and repo are required"
        content, _ = self._fetch_github_readme(owner, repo, branch, lang)
        if content is None:
            return None, None, f"README file not found for '{owner}/{repo}'"
        ref = branch or 'master'
        meta = {
            'source': 'github',
            'github_raw_base': f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/",
        }
        return content, meta, None

    def get_github_catalog(self, installed_plugins):
        """Fetch osysHome-* repositories from GitHub and split into available/enrichment lists."""
        available = []
        enrichments = {}
        osyshome_enrichment = None

        data = self.github_request("https://api.github.com/search/repositories?q=osysHome&per_page=100")
        if not data or data.get('_error') or 'items' not in data:
            message = None
            if isinstance(data, dict):
                if data.get('_error'):
                    message = data.get('message')
                else:
                    message = data.get('message')
            return {
                "available": [],
                "enrichments": {},
                "osyshome_enrichment": None,
                "error": message or "GitHub API unavailable",
            }

        installed_by_name = {plugin['name']: plugin for plugin in installed_plugins}

        for element in data['items']:
            repo_name = element['name']

            if repo_name == 'osysHome':
                osyshome_enrichment = {
                    "stars": element.get('stargazers_count', 0),
                    "owner": "Anisan",
                    "repo": "osysHome",
                    "branch": element.get('default_branch'),
                }
                continue

            parts = repo_name.split('-', 1)
            if len(parts) < 2:
                continue

            short_name = parts[1]
            info = {
                "name": repo_name,
                "repo": repo_name,
                "shortName": short_name,
                "description": element.get('description') or '',
                "author": element['owner']['login'],
                "owner": element['owner']['login'],
                "updated": None,
                "url": element['html_url'],
                "topic": element.get('topics') or [],
                "stars": element.get('stargazers_count', 0),
                "branch": element.get('default_branch'),
                "image": (
                    f"https://raw.githubusercontent.com/{element['owner']['login']}/"
                    f"{repo_name}/{element.get('default_branch')}/static/{short_name}.png"
                ),
            }

            folder_path = os.path.join(Config.PLUGINS_FOLDER, short_name)
            if os.path.isdir(folder_path):
                enrich = {
                    "stars": info["stars"],
                    "owner": info["owner"],
                    "repo": info["repo"],
                    "branch": info["branch"],
                    "image": f"/{short_name}/static/{short_name}.png",
                    "html_url": info["url"],
                }
                installed_plugin = installed_by_name.get(short_name)
                if installed_plugin and installed_plugin.get('author') == 'Undefined':
                    enrich['author'] = info['author']
                enrichments[short_name] = enrich
            else:
                available.append(info)

        return {
            "available": available,
            "enrichments": enrichments,
            "osyshome_enrichment": osyshome_enrichment,
            "error": None,
        }

    def get_github_repo_info(self, owner, repo):
        data = self.github_request(f"https://api.github.com/repos/{owner}/{repo}")
        if not data or data.get('_error'):
            return None
        return data

    def get_github_commit_info(self, owner, repo, commit):
        data = self.github_request(f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}")
        if not data or data.get('_error'):
            return None
        return data

    def download_and_extract_github_repo(self, owner, repo, branch, commit=None, target_folder='.', step_callback=None):
        def step(step_id, status, message=None):
            if step_callback:
                step_callback(step_id, status, message)

        dt = get_now_to_utc()
        if commit is None:
            url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
        else:
            url = f"https://github.com/{owner}/{repo}/archive/{commit}.zip"
            step('fetch_info', 'running')
            info = self.get_github_commit_info(owner, repo, commit)
            if info is None:
                message = self._last_github_error_message(f"Failed to get commit info for {owner}/{repo}:{commit}")
                step('fetch_info', 'error', message)
                raise Exception(message)

            dt = info['commit']['author']['date']
            dt = datetime.datetime.fromisoformat(dt.replace('Z', '+00:00'))
            step('fetch_info', 'done')

        local_filename = f"{repo}.zip"

        step('download', 'running')
        self.logger.info("Downloading %s...", url)
        try:
            response = requests.get(url, timeout=Config.HTTP_REQUEST_TIMEOUT)
        except requests.exceptions.RequestException as ex:
            step('download', 'error', str(ex))
            raise Exception(f"Failed to download the file: {ex}") from ex

        if response.status_code == 403 and 'rate limit' in response.text.lower():
            message = self._friendly_github_error(403, 'API rate limit exceeded')
            step('download', 'error', message)
            raise Exception(message)

        if response.status_code != 200:
            message = f"Failed to download the file: HTTP {response.status_code}"
            step('download', 'error', message)
            raise Exception(message)

        with open(local_filename, 'wb') as f:
            f.write(response.content)
        self.logger.info(f"Downloaded {local_filename}")
        step('download', 'done')

        os.makedirs(target_folder, exist_ok=True)

        temp_extract_folder = os.path.join(target_folder, "temp")
        os.makedirs(temp_extract_folder, exist_ok=True)

        step('extract', 'running')
        self.logger.info(f"Extracting {local_filename} to {temp_extract_folder}...")
        try:
            with zipfile.ZipFile(local_filename, 'r') as zip_ref:
                zip_ref.extractall(temp_extract_folder)
        except (zipfile.BadZipFile, OSError) as ex:
            step('extract', 'error', str(ex))
            raise
        self.logger.info(f"Extracted to {temp_extract_folder}")
        step('extract', 'done')

        step('install_files', 'running')
        if commit is None:
            inner_folder = os.path.join(temp_extract_folder, f"{repo}-{branch}")
        else:
            inner_folder = os.path.join(temp_extract_folder, f"{repo}-{commit}")
        if not os.path.isdir(inner_folder):
            message = f"Archive folder not found: {inner_folder}"
            step('install_files', 'error', message)
            raise Exception(message)
        for item in os.listdir(inner_folder):
            s = os.path.join(inner_folder, item)
            d = os.path.join(target_folder, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)
        step('install_files', 'done')

        step('cleanup', 'running')
        shutil.rmtree(temp_extract_folder)
        os.remove(local_filename)
        self.logger.info(f"Removed {local_filename}")
        step('cleanup', 'done')

        requirements_file = os.path.join(target_folder, 'requirements.txt')
        step('dependencies', 'running')
        if os.path.isfile(requirements_file):
            self.logger.info(f"File {requirements_file} found. Install packets...")
            result = subprocess.run(
                [sys.executable, "-m", "pip", 'install', '-r', requirements_file],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                self.logger.info("Packets installed.")
                step('dependencies', 'done')
            else:
                stderr = (result.stderr or result.stdout or 'pip install failed').strip()
                self.logger.error("Error install packets: %s", stderr)
                step('dependencies', 'error', stderr[:500])
                raise Exception(stderr[:500])
        else:
            self.logger.info(f"File {requirements_file} not found.")
            step('dependencies', 'done', 'skipped')

        return dt

    def search(self, query: str) -> str:
        res = []
        for key,plugin in plugins.items():
            instance = plugin['instance']
            if query.lower() in key.lower() or query.lower() in instance.title.lower() or query.lower() in instance.description.lower():
                res.append({"url":key, "title":f'{instance.title} ({instance.description})', "tags":[{"name":"Module","color":"primary"}]})
        return res

    def widget(self):
        content = {}
        with session_scope() as session:
            res = []
            if getProperty("SystemVar.update"):
                res.append({"title":"osysHome", "img":"/static/assets/images/logo-dark.png",})
            plugins = session.query(Plugin).filter(Plugin.update == True).all()  # noqa
            for plugin in plugins:
                res.append({"title":plugin.title if plugin.title else plugin.name, "img":f"/{plugin.name}/static/{plugin.name}.png"})
            content['update'] = res
        return render_template("widget_modules.html",**content)

    def check_for_new_commits(self, repo_owner, repo_name, last_known_date, branch=None, github_token=None, assume_local_naive=False):
        """
        Проверяет наличие новых коммитов в указанном репозитории GitHub

        Args:
            repo_owner (str): Владелец репозитория
            repo_name (str): Название репозитория
            last_known_date (str/datetime): Дата последнего известного коммита
            branch (str, optional): Название ветки
            github_token (str, optional): Персональный токен GitHub для аутентификации

        Returns:
            tuple: (has_new_commits: bool, latest_commit_date: str, error_message: str)
        """
        # Преобразуем входную дату в timezone-aware datetime (UTC)
        try:
            if isinstance(last_known_date, str):
                last_known_date = datetime.datetime.fromisoformat(last_known_date.replace('Z', '+00:00'))

            if last_known_date.tzinfo is None:
                # For core we store local naive datetime (convert_utc_to_local),
                # so convert to UTC before comparing with GitHub's UTC timestamps.
                if assume_local_naive:
                    last_known_date = convert_local_to_utc(last_known_date).replace(tzinfo=datetime.timezone.utc)
                else:
                    last_known_date = last_known_date.replace(tzinfo=datetime.timezone.utc)
        except (ValueError, AttributeError) as e:
            self.logger.error(f"Invalid date format: {e}")
            return False, None

        # Формируем URL
        base_url = f"https://api.github.com/repos/{repo_owner}/{repo_name}/commits?"

        params = []
        if branch:
            params.append(f"sha={branch}")
        params.append("per_page=1")  # Мы можем проверять только последний коммит

        url = base_url + "&".join(params)

        # Подготавливаем заголовки
        headers = {
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "Python-Commit-Checker"
        }

        if github_token:
            auth = self._github_auth_header(github_token)
            if auth:
                headers["Authorization"] = auth

        try:
            # Сначала проверяем лимиты
            rate_limit_url = "https://api.github.com/rate_limit"
            rate_response = requests.get(rate_limit_url, headers=headers, timeout=Config.HTTP_REQUEST_TIMEOUT)

            if rate_response.status_code == 200:
                rate_data = rate_response.json()
                remaining = rate_data['resources']['core']['remaining']
                reset_time = rate_data['resources']['core']['reset']

                if remaining < 5:
                    wait_time = min(max(reset_time - time.time() + 10, 0), 300)
                    if wait_time > 0 and self._wait_interruptible(wait_time):
                        return False, None

            # Основной запрос
            response = requests.get(url, headers=headers, timeout=Config.HTTP_REQUEST_TIMEOUT)

            # Обрабатываем возможные ошибки
            if response.status_code == 403 and 'rate limit exceeded' in response.text.lower():
                reset_time = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
                wait_time = min(max(reset_time - time.time() + 5, 0), 300)
                if wait_time > 0 and self._wait_interruptible(wait_time):
                    return False, None
                # Повторяем запрос после ожидания
                response = requests.get(url, headers=headers, timeout=Config.HTTP_REQUEST_TIMEOUT)

            response.raise_for_status()

            commits = response.json()

            if not commits:
                return False, None

            # Получаем дату последнего коммита
            latest_commit_date_str = commits[0]['commit']['committer']['date']
            latest_commit_date = datetime.datetime.fromisoformat(latest_commit_date_str.replace('Z', '+00:00'))

            if latest_commit_date.tzinfo is None:
                latest_commit_date = latest_commit_date.replace(tzinfo=datetime.timezone.utc)

            # Сравниваем даты (both UTC-aware)
            if latest_commit_date > last_known_date:
                return True, latest_commit_date.isoformat()
            return False, latest_commit_date.isoformat()

        except requests.exceptions.RequestException as e:
            error_msg = f"Request error: {e}"
            if hasattr(e, 'response') and e.response is not None:
                if e.response.status_code == 403:
                    error_msg += " (Rate limit exceeded)"
                elif e.response.status_code == 404:
                    error_msg += " (Repository not found)"
                elif e.response.status_code == 401:
                    error_msg += " (Authentication failed)"
            self.logger.exception(error_msg)
            return False, None
        except (KeyError, IndexError, ValueError) as e:
            self.logger.exception(f"Data parsing error: {e}")
            return False, None

    def cyclic_task(self):
        if self.event.is_set():
            # Останавливаем цикл
            return

        self.logger.info("Check updates...")
        # get commits for osysHome
        update = getProperty("SystemVar.update")
        if not update:
            branch = getProperty("SystemVar.core_branch")
            last_known = getProperty("SystemVar.upgraded")
            if last_known:
                has_new, new_date = self.check_for_new_commits(
                    repo_owner="Anisan",
                    repo_name="osysHome",
                    last_known_date=last_known,
                    branch=branch,
                    github_token=self.config.get('token',None),
                    assume_local_naive=True,
                )
                if has_new:
                    setProperty("SystemVar.update", True, self.name)
                    self.logger.info(f'Found update for osysHome (branch: {branch})')
            else:
                setProperty("SystemVar.update", True, self.name)
                self.logger.info(f'Found update for osysHome (branch: {branch})')
        repos = {}
        try:
            data = self.github_request("https://api.github.com/search/repositories?q=osysHome&per_page=100")
            if data and not data.get('_error') and 'items' in data:
                for item in data['items']:
                    name = item['name'].split('-')
                    if len(name) > 1:
                        repos[name[1]] = item['html_url']
        except Exception as ex:
            self.logger.exception(ex)

        with session_scope() as session:
            plugins = session.query(Plugin).all()
            for plugin in plugins:
                if plugin.update:
                    continue
                url = plugin.url
                if url is None or url == '':
                    if plugin.name in repos:
                        url = repos[plugin.name]
                if url:
                    owner, repo = self.extract_owner_and_repo(url)
                    if owner is None or repo is None:
                        continue
                else:
                    continue
                has_new = True
                if plugin.updated:
                    last_known = plugin.updated
                    has_new, new_date = self.check_for_new_commits(
                        repo_owner=owner,
                        repo_name=repo,
                        last_known_date=last_known,
                        branch=plugin.branch,
                        github_token=self.config.get('token',None),
                    )
                if has_new:
                    plugin.update = True
                    session.commit()
                    # todo addEvent
                    self.logger.info(f'Found update for "{plugin.name}" (branch: {plugin.branch})')
                    addNotify("Available update",f'Available update module {plugin.name} (branch: {plugin.branch})', CategoryNotify.Info, self.name)

        self.event.wait(60.0 * self.config.get('update_time',60))
