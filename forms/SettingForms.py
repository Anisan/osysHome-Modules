from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField, IntegerField
from wtforms.validators import DataRequired

# Определение класса формы
class SettingsForm(FlaskForm):
    update_time = IntegerField('Update interval (min)', validators=[DataRequired()], default=60)
    token = StringField('Github token')
    submit = SubmitField('Submit')