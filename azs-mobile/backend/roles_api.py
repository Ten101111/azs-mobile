"""Администрирование ролей.

Назначение роли и привязки — операция администратора. Права по списку почт
остаются выше записи в базе: понизить себя случайной правкой нельзя.
"""
from __future__ import annotations

import time
from typing import Callable

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from . import roles


class RoleAssignment(BaseModel):
    role: str = Field(default="", max_length=40)
    binding: str = Field(default="", max_length=400)


class RoleOption(BaseModel):
    value: str
    note: str = ""
    stations: int = 0


class RolesCatalogResponse(BaseModel):
    roles: list[dict]
    options: dict[str, list[RoleOption]]


class AssignmentResult(BaseModel):
    userId: int
    email: str
    role: str
    roleTitle: str
    binding: str
    scopeLabel: str
    scopeStations: int
    problems: list[str] = []


def build_router(require_admin: Callable, require_user: Callable,
                 auth_connection: Callable, user_from_row: Callable) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["roles"])

    @router.get("/roles/catalog", response_model=RolesCatalogResponse)
    def roles_catalog(_admin=Depends(require_admin)):
        options = {
            code: [RoleOption(**item) for item in roles.binding_options(code)]
            for code in roles.ROLE_ORDER
            if roles.ROLES[code].reference_column
        }
        return RolesCatalogResponse(roles=roles.catalog(), options=options)

    @router.get("/roles/preview", response_model=AssignmentResult)
    def roles_preview(role: str = "", binding: str = "", _admin=Depends(require_admin)):
        """Что увидит пользователь с такой ролью — до сохранения."""
        scope = roles.resolve(role, binding)
        spec = roles.describe(role)
        return AssignmentResult(
            userId=0, email="", role=role, roleTitle=spec["title"], binding=binding,
            scopeLabel=scope.label, scopeStations=scope.stations, problems=scope.problems,
        )

    @router.post("/admin/users/{user_id}/role", response_model=AssignmentResult)
    def assign_role(user_id: int, payload: RoleAssignment, admin=Depends(require_admin)):
        role = payload.role.strip()
        binding = payload.binding.strip()

        if role and role not in roles.ROLES:
            raise HTTPException(status_code=400, detail=f"Неизвестная роль: {role}")
        spec = roles.ROLES.get(role)
        if spec and spec.binding_kind != roles.BINDING_NONE and not binding:
            raise HTTPException(
                status_code=400,
                detail=f"Для роли «{spec.title}» нужно указать: {spec.binding_label}",
            )
        if spec and spec.binding_kind == roles.BINDING_NONE:
            binding = ""

        with auth_connection() as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise HTTPException(status_code=404, detail="Пользователь не найден")
            conn.execute(
                "UPDATE users SET role = ?, role_binding = ?, role_assigned_at = ?, "
                "role_assigned_by = ? WHERE id = ?",
                (role, binding, int(time.time()), getattr(admin, "email", ""), user_id),
            )
            conn.commit()
            updated = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()

        profile = user_from_row(updated)
        return AssignmentResult(
            userId=profile.id,
            email=profile.email,
            role=profile.role,
            roleTitle=profile.roleTitle,
            binding=profile.roleBinding,
            scopeLabel=profile.scopeLabel,
            scopeStations=profile.scopeStations,
            problems=profile.scopeProblems,
        )

    @router.get("/me/scope")
    def my_scope(user=Depends(require_user)):
        """Область данных текущего пользователя — для экранов и ИИ."""
        return {
            "role": user.role,
            "roleTitle": user.roleTitle,
            "binding": user.roleBinding,
            "scopeLabel": user.scopeLabel,
            "stations": user.scopeStations,
            "unrestricted": user.unrestricted,
            "aiDialog": user.aiDialog,
            "problems": user.scopeProblems,
        }

    return router
