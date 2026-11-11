"""Запись сущностей CRM в Postgres пачками.

Все функции работают через db.session и не делают commit: транзакцией
управляет вызывающий код (один commit на страницу CRM).
"""
import logging
from collections import defaultdict
from typing import Dict, Iterable, List, Optional

from sqlalchemy import bindparam, delete, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from amocrm import Customer, Lead
from models import AbstractCustomer, AmoCustomer, AmoLead, db

logger = logging.getLogger(__name__)

UPSERT_COLUMNS = ("abstract_customer_id", "login", "name", "general", "api_value", "last_seen_run")


def field_value(entity, name: str):
    field = (entity.custom_fields_values or {}).get(name) or {}
    return field.get("value")


def get_login(entity) -> Optional[str]:
    value = field_value(entity, "Логин")
    return None if value in (None, "") else str(value)


def get_customer_json(customer: Customer) -> dict:
    fields = customer.custom_fields_values or {}
    return {
        "general": {
            "id": customer.id,
            "login": get_login(customer),
            "billing_type": field_value(customer, "Вид оплаты"),
            "is_robot": bool(fields.get("Робот")),
            "is_nds": bool(fields.get("Оплата с НДС")),
            "system": field_value(customer, "Система"),
            "host": field_value(customer, "Хост"),
            "name": customer.name,
        },
        "api_value": customer.as_dict(),
    }


def get_lead_json(lead: Lead) -> dict:
    return {
        "general": {
            "id": lead.id,
            "login": get_login(lead),
            "name": lead.name,
        },
        "api_value": lead.as_dict(),
    }


def is_marked_for_delete(name: Optional[str]) -> bool:
    return "удалить" in (name or "").lower()


def entity_row(data: dict, run_id: int) -> dict:
    """Строка для amo_customer / amo_lead; abstract_customer_id проставляется позже.

    Сущность с «удалить» в имени не получает метку прогона, поэтому чистка
    удалит её так же, как сущность, которой не было в выгрузке.
    """
    general = data["general"]
    return {
        "id": general["id"],
        "abstract_customer_id": None,
        "login": general["login"],
        "name": general["name"],
        "general": general,
        "api_value": data["api_value"],
        "last_seen_run": None if is_marked_for_delete(general["name"]) else run_id,
    }


# --- запись ----------------------------------------------------------------

def upsert(model, rows: Iterable[dict]) -> None:
    rows = list(rows)
    if not rows:
        return
    stmt = insert(model).values(rows)
    stmt = stmt.on_conflict_do_update(
        index_elements=[model.id],
        set_={column: stmt.excluded[column] for column in UPSERT_COLUMNS},
    )
    db.session.execute(stmt)


def create_abstract_customers(count: int) -> List[int]:
    """Создать count пустых клиентов одним запросом и вернуть их id """
    if count == 0:
        return []
    stmt = (
        insert(AbstractCustomer)
        .from_select([AbstractCustomer.created_at],
                     select(func.now()).select_from(func.generate_series(1, count)))
        .returning(AbstractCustomer.id)
    )
    return list(db.session.scalars(stmt))


def dict_compare_merge(first: Optional[list], second: Optional[list]) -> list:
    """Объединить списки сущностей вида {"general": {"id": ...}} по id."""
    merged: Dict[object, dict] = defaultdict(dict)
    for item in (first or []) + (second or []):
        merged[(item.get("general") or {}).get("id")].update(item)
    return list(merged.values())


def merge_send_methods(first: Optional[list], second: Optional[list]) -> list:
    result, seen = [], set()
    for item in (first or []) + (second or []):
        key = (item.get("type"), item.get("value"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def merge_abstract_customers(keep_id: int, drop_id: int) -> None:
    """Слить клиента drop_id в keep_id: перенести сущности и JSON, удалить drop_id."""
    session = db.session
    rows = {row.id: row for row in session.execute(
        select(AbstractCustomer.id, AbstractCustomer.send_methods,
               AbstractCustomer.amo_contacts, AbstractCustomer.amo_companies)
        .where(AbstractCustomer.id.in_([keep_id, drop_id]))
    )}
    keep, drop = rows[keep_id], rows[drop_id]

    for model in (AmoCustomer, AmoLead):
        session.execute(update(model)
                        .where(model.abstract_customer_id == drop_id)
                        .values(abstract_customer_id=keep_id))

    session.execute(update(AbstractCustomer)
                    .where(AbstractCustomer.id == keep_id)
                    .values(send_methods=merge_send_methods(keep.send_methods, drop.send_methods),
                            amo_contacts=dict_compare_merge(keep.amo_contacts, drop.amo_contacts),
                            amo_companies=dict_compare_merge(keep.amo_companies, drop.amo_companies)))

    session.execute(delete(AbstractCustomer).where(AbstractCustomer.id == drop_id))
    logger.info("Клиент %s слит в %s", drop_id, keep_id)



def delete_not_seen(model, run_id: int) -> int:
    """Удалить сущности без метки этого прогона: их не было в выгрузке или в имени «удалить»."""
    result = db.session.execute(delete(model).where(model.last_seen_run.is_distinct_from(run_id)))
    return result.rowcount


def delete_empty_abstract_customers() -> int:
    """Удалить клиентов, у которых не осталось ни покупателей, ни сделок."""
    result = db.session.execute(
        delete(AbstractCustomer).where(
            ~exists().where(AmoCustomer.abstract_customer_id == AbstractCustomer.id),
            ~exists().where(AmoLead.abstract_customer_id == AbstractCustomer.id),
        )
    )
    return result.rowcount


def filter_merge_abstract_customer(items: Optional[list], check_ids: set) -> Optional[list]:
    """Оставить вложенные сущности, которые были в выгрузке и без «удалить» в имени."""
    if items is None:
        return None
    return [item for item in items
            if (item.get("general") or {}).get("id") in check_ids
            and not is_marked_for_delete((item.get("general") or {}).get("name"))]


def filter_abc_customer(ids_map: Dict[int, dict]) -> int:
    """Отфильтровать контакты и компании клиентов; записать только изменившихся.

    ids_map: id клиента -> {"contacts": set(id), "companies": set(id)} из выгрузки.
    """
    empty = {"contacts": set(), "companies": set()}
    changes = []
    stmt = (select(AbstractCustomer.id, AbstractCustomer.amo_contacts, AbstractCustomer.amo_companies)
            .where(or_(AbstractCustomer.amo_contacts.isnot(None),
                       AbstractCustomer.amo_companies.isnot(None)))
            .execution_options(yield_per=1000))
    for abc_id, contacts, companies in db.session.execute(stmt):
        seen = ids_map.get(abc_id, empty)
        new_contacts = filter_merge_abstract_customer(contacts, seen["contacts"])
        new_companies = filter_merge_abstract_customer(companies, seen["companies"])
        if new_contacts != contacts or new_companies != companies:
            changes.append({"_id": abc_id, "_contacts": new_contacts, "_companies": new_companies})

    if changes:
        db.session.execute(
            update(AbstractCustomer)
            .where(AbstractCustomer.id == bindparam("_id"))
            .values(amo_contacts=bindparam("_contacts"), amo_companies=bindparam("_companies")),
            changes,
        )
    return len(changes)
