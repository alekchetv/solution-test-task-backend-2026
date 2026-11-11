"""Индекс в памяти: какому AbstractCustomer принадлежат покупатели и сделки CRM.

Загружается двумя запросами в начале прогона, дальше поиск идёт по словарям
без запросов к Postgres.
"""
import logging
from collections import defaultdict
from typing import Dict, Optional, Set, Tuple

from sqlalchemy import select

from models import AmoCustomer, AmoLead

logger = logging.getLogger(__name__)


class OwnerIndex:
    def __init__(self):
        self.customers: Dict[int, Tuple[int, Optional[str]]] = {}
        self.leads: Dict[int, Tuple[int, Optional[str]]] = {}
        self.customers_by_login: Dict[str, Set[int]] = defaultdict(set)
        self.leads_by_login: Dict[str, Set[int]] = defaultdict(set)
        self.merged: Dict[int, int] = {}

    @classmethod
    def load(cls, session) -> "OwnerIndex":
        index = cls()
        for row in session.execute(select(AmoCustomer.id, AmoCustomer.abstract_customer_id,
                                          AmoCustomer.login)):
            index.save_customer(*row)
        for row in session.execute(select(AmoLead.id, AmoLead.abstract_customer_id,
                                          AmoLead.login)):
            index.save_lead(*row)
        return index

    def resolve(self, abstract_id: int) -> int:
        """Актуальный id клиента с учётом слияний."""
        while abstract_id in self.merged:
            abstract_id = self.merged[abstract_id]
        return abstract_id

    def merge(self, keep_id: int, drop_id: int) -> None:
        self.merged[self.resolve(drop_id)] = self.resolve(keep_id)

    # --- поиск ---

    def find_customer(self, crm_id: int, login: Optional[str]) -> Optional[int]:
        """По id покупателя, иначе по логину среди сделок."""
        if crm_id in self.customers:
            return self.resolve(self.customers[crm_id][0])
        return self._one_by_login(self.leads, self.leads_by_login, login)

    def find_lead(self, crm_id: int, login: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
        """(клиент по id сделки, клиент по логину среди покупателей)."""
        by_id = self.resolve(self.leads[crm_id][0]) if crm_id in self.leads else None
        by_login = self._one_by_login(self.customers, self.customers_by_login, login)
        return by_id, by_login

    def _one_by_login(self, entities, by_login, login) -> Optional[int]:
        """Клиент с таким логином, если он ровно один."""
        if not login:
            return None
        owners = {self.resolve(entities[crm_id][0]) for crm_id in by_login.get(login, ())}
        if len(owners) > 1:
            logger.warning("Логин %r у нескольких клиентов %s, совпадение не используется",
                           login, sorted(owners))
            return None
        return owners.pop() if owners else None

    # --- запись ---

    def save_customer(self, crm_id: int, abstract_id: int, login: Optional[str]) -> None:
        self._save(self.customers, self.customers_by_login, crm_id, abstract_id, login)

    def save_lead(self, crm_id: int, abstract_id: int, login: Optional[str]) -> None:
        self._save(self.leads, self.leads_by_login, crm_id, abstract_id, login)

    def _save(self, entities, by_login, crm_id, abstract_id, login) -> None:
        old = entities.get(crm_id)
        if old and old[1]:
            by_login[old[1]].discard(crm_id)   # логин сменился — убрать старый
        entities[crm_id] = (self.resolve(abstract_id), login)
        if login:
            by_login[login].add(crm_id)
