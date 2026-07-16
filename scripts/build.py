from .const import bins_dir


def prep():
    bin_list = []

    for bin_dir in bins_dir.iterdir():
        if not bin_dir.is_dir():
            continue

        bin_list.append(bin_dir.name)

    with open("marcuson_python_scripts/bin_list.py", "w") as f:
        f.write(f"bins = {repr(bin_list)}")
