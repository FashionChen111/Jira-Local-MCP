"""Single-line configuration syntax must preserve literal credentials."""
import pytest
from env_file import parse_env


def test_comments_quotes_export_and_literal_values():
    assert parse_env('\ufeff# comment\r\nexport A="a # $HOME"\r\nB=abc#123 # note\r\nC=\'a"b\'\n') == {
        'A': 'a # $HOME', 'B': 'abc#123', 'C': 'a"b',
    }


@pytest.mark.parametrize('text', ['BROKEN_SECRET', 'KEY="SECRET', 'BAD KEY=SECRET', 'KEY="SECRET" garbage'])
def test_invalid_line_reports_number_without_secret(text):
    with pytest.raises(ValueError) as error:
        parse_env('# header\n' + text)
    assert '2' in str(error.value)
    assert 'SECRET' not in str(error.value)
