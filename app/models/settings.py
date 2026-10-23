from app.extensions import db


class SiteSetting(db.Model):
    __tablename__ = "site_settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    value = db.Column(db.Text, nullable=False, default="")

    @staticmethod
    def get(key: str, default: str = "") -> str:
        row = SiteSetting.query.filter_by(key=key).first()
        return row.value if row else default

    @staticmethod
    def set(key: str, value: str) -> None:
        row = SiteSetting.query.filter_by(key=key).first()
        if row is None:
            row = SiteSetting(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value
        db.session.commit()