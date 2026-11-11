from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import Column, func, MetaData, ForeignKey
from sqlalchemy.dialects.postgresql import JSONB

naming_convention = {
    "ix": 'ix_%(column_0_label)s',
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s"
}
db = SQLAlchemy(metadata=MetaData(naming_convention=naming_convention))


class AbstractCustomer(db.Model):
    __tablename__ = "abstract_customer"
    id = Column(db.BigInteger, primary_key=True)

    amo_companies = Column(JSONB)
    amo_contacts = Column(JSONB)
    send_methods = Column(JSONB)
    created_at = Column(db.DateTime, server_default=func.now())
    updated_at = Column(db.DateTime, onupdate=func.now())

    customers = db.relationship("AmoCustomer", back_populates="abstract_customer",
                                passive_deletes=True)
    leads = db.relationship("AmoLead", back_populates="abstract_customer",
                            passive_deletes=True)


class AmoCustomer(db.Model):
    """
    "general": {
        id: int,
        login: str,
        billing_type: str,
        is_robot: bool,
        is_nds: bool,
        system: str,
        host: str,
        name: str,
    },
    "api_value": {}
    """
    __tablename__ = "amo_customer"
    id = Column(db.BigInteger, primary_key=True,
                autoincrement=False)  # id из CRM
    abstract_customer_id = Column(db.BigInteger,
                                  ForeignKey("abstract_customer.id",
                                             ondelete="CASCADE"),
                                  nullable=False, index=True)
    login = Column(db.Text, nullable=False, index=True)
    name = Column(db.Text, index=True)
    general = Column(JSONB)
    api_value = Column(JSONB)
    last_seen_run = Column(db.BigInteger)

    abstract_customer = db.relationship(
        "AbstractCustomer", back_populates="customers")


class AmoLead(db.Model):
    """
    general: {
        id: int, 
        login: str, 
        name: str
    }
    """
    __tablename__ = "amo_lead"
    id = Column(db.BigInteger, primary_key=True, autoincrement=False)
    abstract_customer_id = Column(db.BigInteger,
                                  ForeignKey("abstract_customer.id",
                                             ondelete="CASCADE"),
                                  nullable=False, index=True)
    login = Column(db.Text, index=True)
    name = Column(db.Text, index=True)
    general = Column(JSONB)
    api_value = Column(JSONB)
    last_seen_run = Column(db.BigInteger)

    abstract_customer = db.relationship(
        "AbstractCustomer", back_populates="leads")
