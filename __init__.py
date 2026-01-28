import requests
import re
import zipfile
import os
import sys
import time
import datetime
import shutil
import subprocess
from app.configuration import Config
from flask import render_template, redirect, jsonify
from sqlalchemy import text
from app.core.main.BasePlugin import BasePlugin
from app.core.main.PluginsHelper import plugins
from app.core.models.Plugins import Plugin
from app.database import db, session_scope, get_now_to_utc, convert_utc_to_local
from app.core.lib.common import addNotify, CategoryNotify
from app.core.lib.object import setProperty, getProperty
from plugins.Modules.forms.SettingForms import SettingsForm

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
                module = Plugin()
                module.name = name
                module.updated = dt
                module.url = f'https://github.com/{owner}/{repo}'
                module.save()
                db.session.commit()
                addNotify("Success install",f'Success install module {name}',CategoryNotify.Info,self.name)
                setProperty("SystemVar.NeedRestart", True, self.name)
            except Exception as ex:
                self.logger.exception(ex)
                addNotify("Error install",f'Error install module {name}',CategoryNotify.Error,self.name)

            return redirect(self.name)

        if op == 'uninstall':
            name = request.args.get('name',None)
            self.logger.info(f"Unstalling module '{name}'")

            if name in ['Modules','Objects','Users','Scheduler','wsServer']:
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
            "token": self.config.get('token',''),
        }
        return self.render("modules.html", content)

    def extract_owner_and_repo(self, url):
        pattern = r"https://github\.com/([^/]+)/([^/]+)"
        match = re.match(pattern, url)

        if match:
            owner, repo = match.groups()
            return owner, repo
        else:
            return None, None

    def github_request(self, url):
        github_headers = {
            'Accept': 'application/vnd.github.v3+json',
            'User-Agent': 'Python-Commit-Checker',
        }
        token = self.config.get('token',None)
        if token:
            github_headers['Authorization'] = f'token {token}'
        response = requests.get(url, headers=github_headers)
        if response.status_code == 200:
            return response.json()
        else:
            return None

    def get_github_repo_info(self, owner, repo):
        return self.github_request(f"https://api.github.com/repos/{owner}/{repo}")

    def get_github_commit_info(self, owner, repo, commit):
        return self.github_request(f"https://api.github.com/repos/{owner}/{repo}/commits/{commit}")

    def download_and_extract_github_repo(self, owner, repo, branch, commit=None, target_folder='.'):
        dt = get_now_to_utc()
        if commit is None:
            url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
        else:
            url = f"https://github.com/{owner}/{repo}/archive/{commit}.zip"
            info = self.get_github_commit_info(owner,repo,commit)
            if info is None:
                raise Exception(f"Failed to get commit info for {owner}/{repo}:{commit}")

            dt = info['commit']['author']['date']
            dt = datetime.datetime.fromisoformat(dt.replace('Z', '+00:00'))

        local_filename = f"{repo}.zip"

        # Скачивание архива
        self.logger.info("Downloading %s...",url)
        response = requests.get(url)

        if response.status_code == 200:
            with open(local_filename, 'wb') as f:
                f.write(response.content)
            self.logger.info(f"Downloaded {local_filename}")
        else:
            raise Exception(f"Failed to download the file: {response.status_code}")

        # Создание целевой папки
        os.makedirs(target_folder, exist_ok=True)

        # Распаковка архива во временную папку
        temp_extract_folder = os.path.join(target_folder, "temp")
        os.makedirs(temp_extract_folder, exist_ok=True)

        self.logger.info(f"Extracting {local_filename} to {temp_extract_folder}...")
        with zipfile.ZipFile(local_filename, 'r') as zip_ref:
            zip_ref.extractall(temp_extract_folder)
        self.logger.info(f"Extracted to {temp_extract_folder}")

        # Перемещение файлов из внутренней папки архива в целевую папку
        if commit is None:
            inner_folder = os.path.join(temp_extract_folder, f"{repo}-{branch}")
        else:
            inner_folder = os.path.join(temp_extract_folder, f"{repo}-{commit}")
        for item in os.listdir(inner_folder):
            s = os.path.join(inner_folder, item)
            d = os.path.join(target_folder, item)
            if os.path.isdir(s):
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                shutil.copy2(s, d)

        # Удаление временной папки и загруженного архива
        shutil.rmtree(temp_extract_folder)

        # Удаление загруженного архива
        os.remove(local_filename)
        self.logger.info(f"Removed {local_filename}")

        # Полный путь к файлу requirements.txt
        requirements_file = os.path.join(target_folder, 'requirements.txt')

        # Проверяем наличие файла requirements.txt
        if os.path.isfile(requirements_file):
            self.logger.info(f"File {requirements_file} found. Install packets...")

            # Выполнение команды установки зависимостей
            result = subprocess.run([sys.executable, "-m", "pip", 'install', '-r', requirements_file], capture_output=True, text=True)

            if result.returncode == 0:
                self.logger.info("Packets installed.")
            else:
                self.logger.error("Error install packets: %s", result.stderr)
        else:
            self.logger.info(f"File {requirements_file} not found.")

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

    def check_for_new_commits(self, repo_owner, repo_name, last_known_date, branch=None, github_token=None):
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
        # Преобразуем входную дату в timezone-aware datetime
        try:
            if isinstance(last_known_date, str):
                last_known_date = datetime.datetime.fromisoformat(last_known_date.replace('Z', '+00:00'))

            if last_known_date.tzinfo is None:
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
            headers["Authorization"] = f"token {github_token}"

        try:
            # Сначала проверяем лимиты
            rate_limit_url = "https://api.github.com/rate_limit"
            rate_response = requests.get(rate_limit_url, headers=headers)

            if rate_response.status_code == 200:
                rate_data = rate_response.json()
                remaining = rate_data['resources']['core']['remaining']
                reset_time = rate_data['resources']['core']['reset']

                if remaining < 5:
                    wait_time = reset_time - time.time() + 10  # Добавляем 10 секунд буфера
                    if wait_time > 0:
                        time.sleep(wait_time)

            # Основной запрос
            response = requests.get(url, headers=headers)

            # Обрабатываем возможные ошибки
            if response.status_code == 403 and 'rate limit exceeded' in response.text.lower():
                reset_time = int(response.headers.get('X-RateLimit-Reset', time.time() + 60))
                wait_time = reset_time - time.time() + 5  # 5 секунд буфера
                if wait_time > 0:
                    time.sleep(wait_time)
                # Повторяем запрос после ожидания
                response = requests.get(url, headers=headers)

            response.raise_for_status()

            commits = response.json()

            if not commits:
                return False, None

            # Получаем дату последнего коммита
            latest_commit_date_str = commits[0]['commit']['committer']['date']
            latest_commit_date = datetime.datetime.fromisoformat(latest_commit_date_str.replace('Z', '+00:00'))

            if latest_commit_date.tzinfo is None:
                latest_commit_date = latest_commit_date.replace(tzinfo=datetime.timezone.utc)

            # Сравниваем даты
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
            data = data['items']
            for item in data:
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
