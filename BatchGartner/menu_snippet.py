# Batch Gartner integration
try:
    import BatchGartner_NukeIntegration as _bg

    _bg.install_menu()
except Exception as _bg_error:
    print("Batch Gartner integration:", _bg_error)
