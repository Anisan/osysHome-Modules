import requests, re
import zipfile
import os
import datetime
import shutil
from settings import Config
from flask import render_template, redirect
from app.core.main.BasePlugin import BasePlugin
from app.core.main.PluginsHelper import plugins
from app.core.models.Plugins import Plugin
from app.database import db, row2dict
from app.core.lib.common import addNotify, CategoryNotify
from plugins.Modules.forms.ModuleForm import routeSettings

class Modules(BasePlugin):

    def __init__(self,app):
        super().__init__(app,__name__)
        self.title = "Modules"
        self.description = """List modules"""
        self.category = "System"
    
    def initialization(self):
        pass

    def admin(self, request):

        op = request.args.get("op",None)

        if op == 'settings':

            return routeSettings(request)
        
        if op == 'upgrade':
            name = request.args.get('name',None)
            module = Plugin.query.filter(Plugin.name == name).one_or_404()
            url = module.url

            owner, repo = self.extract_owner_and_repo(url)
            try:
                self.download_and_extract_github_repo(owner, repo, "master", os.path.join(Config.PLUGINS_FOLDER,name))
                module.updated = datetime.datetime.now()
                db.session.commit()
                addNotify("Success update",f'Success update module {name}',CategoryNotify.Info,self.name)
            except Exception as ex:
                self.logger.exception(ex)
                addNotify("Error update",f'Error update module {name}',CategoryNotify.Error,self.name)

            return redirect(self.name)

        rows = Plugin.query.all()
        rows = list(map(lambda x: row2dict(x), rows))
        
        for item in rows:
            if item["active"]:
                if item["name"] in plugins:
                    module = plugins[item['name']]
                    item["installed"] = True
                    if not item['title']:
                        item["title"] = module["instance"].title
                    if not item['category']:
                        item["category"] = module["instance"].category
                    item["description"] = module["instance"].description
                    item["version"] = module["instance"].version
                    item["actions"] = module["instance"].actions
                    item["alive"] = module["instance"].is_alive()
                    if "cycle" in item["actions"]:
                        item["updated"] = module["instance"].dtUpdated
                else:
                    item["installed"] = False

                item["new"] = False
                if item["url"]:
                    owner, repo = self.extract_owner_and_repo(item['url'])
                    adv_info = self.get_github_repo_info(owner,repo)
                    info = { 
                        "owner": owner,
                        "repo": repo,
                        "updated": datetime.datetime.strptime(adv_info['updated_at'], "%Y-%m-%dT%H:%M:%SZ")
                    }
                    item['info'] = info
                    if item["updated"]:
                        if item["updated"] < item['info']['updated']:
                            item["new"] = True
                    else:
                        item["new"] = True
            else:
                item["title"] = item["name"]
        content = {
            "plugins": rows,
        }
        return render_template("modules.html", **content)
        
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

