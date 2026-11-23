

def test_an_unquoted_off_means_off_and_a_bad_setting_is_a_readable_error(tmp_path) -> None:
    import pytest

    from dridh.common.errors import DridhError, ErrorCode
    from dridh.config.dridh_yaml import create_default_config, load_config, save_config

    path = tmp_path / "dridh.yaml"
    save_config(path, create_default_config(tmp_path, workspace_id="w"))
    text = path.read_text(encoding="utf-8")
    assert "mode: auto" in text
    path.write_text(text.replace("mode: auto", "mode: off"), encoding="utf-8")  # YAML reads this as false
    assert load_config(path).execution.containers.mode == "off"
    path.write_text(text.replace("mode: auto", "mode: sometimes"), encoding="utf-8")
    with pytest.raises(DridhError) as raised:
        load_config(path)
    assert raised.value.code is ErrorCode.CONFIGURATION_ERROR and isinstance(raised.value, ValueError)
    assert "execution.containers.mode" in raised.value.details[0] and "sometimes" in raised.value.details[0]
