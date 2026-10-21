with open("src/model_client.py") as f:
    lines = f.readlines()
lines[28] = """Outcome of evaluating one model output."""
with open("src/model_client.py","w") as f:
    f.writelines(lines)
print("docstring fixed")