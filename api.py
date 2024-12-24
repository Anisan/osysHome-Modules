from flask import  request
from flask_restx import Namespace, Resource, fields
from app.api.decorators import api_key_required, role_required
from app.api.models import model_404, model_result
from app.core.models.Plugins import Plugin
from app.database import row2dict, session_scope
from app.core.main.PluginsHelper import plugins
from app.core.lib.object import getProperty

_api_ns = Namespace(name="Modules", description="Modules namespace", validate=True)

response_result = _api_ns.model("Result", model_result)
response_404 = _api_ns.model("Error", model_404)

# Модель данных для Swagger
module_model = _api_ns.model('Module', {
    'name': fields.String(required=True, description='The name of the module')
})

def create_api_ns():
    return _api_ns

@_api_ns.route("/cycle/start")
class StartCycle(Resource):
    @api_key_required
    @role_required("admin")
    @_api_ns.doc(security="apikey")
    @_api_ns.expect(module_model)
    def post(self):
        """
        Start cycle by name module.
        """
        data = request.json
        module_name = data.get('name')

        if not module_name:
            return {"message": "Service name is required"}, 400

        if module_name in plugins:
            module = plugins[module_name]
            if module["instance"].is_alive():
                return {"message": f"Cycle '{module_name}' is already running", "status": "ok"}, 200
            module["instance"].start_cycle()
            return {"message": f"Cycle '{module_name}' started", "status": "ok"}, 200
        else:
            return {"message": f"Module '{module_name}' not found", "status": "error"}, 404


@_api_ns.route("/cycle/stop")
class StopCycle(Resource):
    @api_key_required
    @role_required("admin")
    @_api_ns.doc(security="apikey")
    @_api_ns.expect(module_model)
    def post(self):
        """
        Stop cycle by name module.
        """
        data = request.json
        module_name = data.get('name')

        if not module_name:
            return {"message": "Service name is required"}, 400

        if module_name in plugins:
            module = plugins[module_name]
            if not module["instance"].is_alive():
                return {"message": f"Cycle '{module_name}' is already stopped", "status": "ok"}, 200
            module["instance"].stop_cycle()
            return {"message": f"Cycle '{module_name}' stopped", "status": "ok"}, 200
        else:
            return {"message": f"Module '{module_name}' not found", "status": "error"}, 404

@_api_ns.route("/cycle/restart")
class RestartCycle(Resource):
    @api_key_required
    @role_required("admin")
    @_api_ns.doc(security="apikey")
    def post(self):
        """
        Restart cycle by name module.
        """
        data = request.json
        module_name = data.get('name')

        if not module_name:
            return {"message": "Service name is required"}, 400

        if module_name in plugins:
            module = plugins[module_name]
            module["instance"].stop_cycle()
            module["instance"].start_cycle()
            return {"message": f"Cycle '{module_name}' stopped", "status": "ok"}, 200
        else:
            return {"message": f"Module '{module_name}' not found", "status": "error"}, 404

@_api_ns.route("/plugins")
class GetPlugins(Resource):
    @api_key_required
    @role_required("admin")
    @_api_ns.doc(security="apikey")
    @_api_ns.response(200, "List plugins", response_result)
    def get(self):
        """
        Get plugins
        """
        with session_scope() as session:
            ps = session.query(Plugin).order_by(Plugin.name).all()
            result = [row2dict(plugin) for plugin in ps]
            for item in result:
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
                        item["author"] = module["instance"].author
                        item["alive"] = module["instance"].is_alive()
                        item['new'] = False
                        if "cycle" in item["actions"]:
                            item["updatedCycle"] = module["instance"].dtUpdated
                    else:
                        item["installed"] = False
                else:
                    item["title"] = item["name"]
            osysHome = {
                "title": "osysHome",
                "name": "osysHome",
                "description":"Object System smartHome",
                "topic":["core","smarthome"],
                "new": False,
                "author":"Eraser",
                "updated":getProperty("SystemVar.updated"),
                "url":"https://github.com/Anisan/osysHome",
            }
            return {"success": True, "result": result, "osysHome":osysHome}, 200



