from pawchive_app import ui
from pawchive_app.enhancements import install_ui_enhancements


install_ui_enhancements(ui)


if __name__ == "__main__":
    raise SystemExit(ui.run_app())
