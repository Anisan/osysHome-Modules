import requests
import re
import zipfile
import os
import sys
import datetime
import shutil
import subprocess
from settings import Config
from flask import render_template, redirect
from sqlalchemy import text
from app.core.main.BasePlugin import BasePlugin
from app.core.main.PluginsHelper import plugins
from app.core.models.Plugins import Plugin
from app.database import db, session_scope
from app.core.lib.common import addNotify, CategoryNotify
from app.core.lib.object import setProperty

class Modules(BasePlugin):

    def __init__(self,app):
        super().__init__(app,__name__)
        self.title = "Modules"
        self.description = """List modules"""
        self.category = "System"
        self.actions = ["search"]
        self.author = "Eraser"

    def initialization(self):
        pass

    def admin(self, request):

        op = request.args.get("op",None)

        if op == 'upgrade':
            name = request.args.get('name',None)
            url = request.args.get('url',None)
            module = Plugin.query.filter(Plugin.name == name).one_or_404()
            if module and module.url:
                url = module.url

            owner, repo = self.extract_owner_and_repo(url)
            try:
                info = self.get_github_repo_info(owner, repo)
                self.download_and_extract_github_repo(owner, repo, info['default_branch'], os.path.join(Config.PLUGINS_FOLDER,name))
                module.updated = datetime.datetime.now()
                db.session.commit()
                addNotify("Success update",f'Success update module {name}',CategoryNotify.Info,self.name)
                setProperty("SystemVar.NeedRestart", True, self.name)
            except Exception as ex:
                self.logger.exception(ex)
                addNotify("Error update",f'Error update module {name}',CategoryNotify.Error,self.name)

            return redirect(self.name)

        if op == 'upgrade_core':
            owner = 'Anisan'
            repo = 'osysHome'
            branch = 'master'
            try:
                self.download_and_extract_github_repo(owner, repo, branch, os.path.join(Config.APP_DIR))
                setProperty("SystemVar.upgraded",datetime.datetime.now(),self.name)
                addNotify("Success update", 'Success update osysHome',CategoryNotify.Info,self.name)
                setProperty("SystemVar.NeedRestart", True, self.name)
            except Exception as ex:
                self.logger.exception(ex)
                addNotify("Error update", 'Error update osysHome',CategoryNotify.Error,self.name)

            return redirect(self.name)

        if op == 'install':
            name = request.args.get('name',None)
            owner = request.args.get('author',None)
            repo = 'osysHome-' + name

            try:
                info = self.get_github_repo_info(owner, repo)
                self.download_and_extract_github_repo(owner, repo, info['default_branch'], os.path.join(Config.PLUGINS_FOLDER,name))
                module = Plugin()
                module.name = name
                module.updated = datetime.datetime.now()
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

        return render_template("modules.html")

    def extract_owner_and_repo(self, url):
        pattern = r"https://github\.com/([^/]+)/([^/]+)"
        match = re.match(pattern, url)

        if match:
            owner, repo = match.groups()
            return owner, repo
        else:
            return None, None

    def get_github_repo_info(self, owner, repo):
        url = f"https://api.github.com/repos/{owner}/{repo}"
        response = requests.get(url)

        if response.status_code == 200:
            return response.json()
        else:
            return None

    def download_and_extract_github_repo(self, owner, repo, branch, target_folder='.'):
        url = f"https://github.com/{owner}/{repo}/archive/refs/heads/{branch}.zip"
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
        inner_folder = os.path.join(temp_extract_folder, f"{repo}-{branch}")
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
            # Устанавливаем пакеты из requirements.txt с помощью pip
            # Определение пути к pip в зависимости от операционной системы
            if sys.platform == "win32":
                pip_path = os.path.join('venv', 'Scripts', 'pip')
            else:
                pip_path = os.path.join('venv', 'bin', 'pip')

            # Выполнение команды установки зависимостей
            result = subprocess.run([pip_path, 'install', '-r', requirements_file], capture_output=True, text=True)

            if result.returncode == 0:
                self.logger.info("Packets installed.")
            else:
                self.logger.error("Error install packets: %s", result.stderr)
        else:
            self.logger.info(f"File {requirements_file} not found.")

    def search(self, query: str) -> str:
        res = []
        for key,plugin in plugins.items():
            instance = plugin['instance']
            if query.lower() in key.lower() or query.lower() in instance.title.lower() or query.lower() in instance.description.lower():
                res.append({"url":key, "title":f'{instance.title} ({instance.description})', "tags":[{"name":"Module","color":"primary"}]})
        return res
