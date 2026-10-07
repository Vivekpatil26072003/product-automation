from enum import StrEnum


class Unit(StrEnum):
    M = "m"
    KG = "kg"
    PCS = "pcs"


class Status(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PENDING = "PENDING"
    HOLD = "HOLD"


class Role(StrEnum):
    UPLOADER = "UPLOADER"
    REVIEWER = "REVIEWER"
    SENDER = "SENDER"
    ADMIN = "ADMIN"
    VIEWER = "VIEWER"


# Unit dimension: m is length, kg is mass, pcs is count. Different dimensions never combine.
UNIT_DIMENSION = {Unit.M: "length", Unit.KG: "mass", Unit.PCS: "count"}
