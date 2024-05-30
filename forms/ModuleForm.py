from flask import render_template, redirect
from flask_wtf import  FlaskForm
from wtforms import StringField, SubmitField, BooleanField
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
    submit = SubmitField('Submit')

def routeSettings(request):
    name = request.args.get('name',None)
    module = Plugin.query.filter(Plugin.name == name).one_or_404()
    form = ModuleForm(obj=module)

    if form.validate_on_submit():
        form.populate_obj(module)
        db.session.commit()
        cache.delete('sidebar')
        return redirect("Modules")
    
    return render_template("module.html", name=name, form=form)