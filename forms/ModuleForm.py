import json
from flask import render_template, redirect
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, BooleanField, SelectField
from wtforms.validators import DataRequired
from app.extensions import cache
from app.database import db
from app.core.models.Plugins import Plugin

class ModuleForm(FlaskForm):
    title = StringField('Name')
    category = StringField('Category')
    hidden = BooleanField('Hidden in statusbar')
    active = BooleanField('Active')
    url = StringField('Url repository Github')
    level_logging = SelectField("Level logging (apply after restart)",
                                choices=(
                                    (None,"Default"),
                                    ("DEBUG","Debug"),
                                    ("INFO","Info"),
                                    ("WARNING","Warning"),
                                    ("ERROR","Error"),
                                    ("CRITICAL","Critical")
                                ),
                                validators=[DataRequired()])
    submit = SubmitField('Submit')

def routeSettings(request):
    name = request.args.get('name',None)
    module = Plugin.query.filter(Plugin.name == name).one_or_404()
    form = ModuleForm(obj=module)
    config = {}
    if module.config:
        config = json.loads(module.config)

    if form.validate_on_submit():
        form.populate_obj(module)
        config['level_logging'] = form.level_logging.data
        module.config = json.dumps(config)
        db.session.commit()
        cache.delete('sidebar')
        return redirect("Modules")

    form.level_logging.data = config.get('level_logging', None)
    return render_template("module.html", name=name, form=form)
