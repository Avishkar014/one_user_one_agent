import sys
sys.path.insert(0, "d:/One_User_One_agent/stage-one-user-one-agent/src")
from src.data_loader import DataPack, LoadReport

p = DataPack(paths=None, report=LoadReport())
print("events:", p.events)
print("triggers:", p.triggers)
print("holdout_users:", p.holdout_users)
print("events type:", type(p.events))