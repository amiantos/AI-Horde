from horde.logger import logger
from horde.flask import db


class HordeSettings(db.Model):
    """For storing settings"""
    __tablename__ = "settings"
    id = db.Column(db.Integer, primary_key=True)
    raid = db.Column(db.Boolean, default=False, nullable=False)
    invite_only = db.Column(db.Boolean, default=False, nullable=False)
    maintenance = db.Column(db.Boolean, default=False, nullable=False)
