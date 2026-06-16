"""Tests for EnergyLedger — deleted messages retain energy data."""
import pytest
from app.models import Thread, Message, EnergyLedger
from app import db


def _make_thread(db, user, title="Test Thread"):
    t = Thread(user_id=user.id, title=title)
    db.session.add(t)
    db.session.commit()
    return t


def _make_msg(db, thread, role="assistant", tokens=100, energy=0.5, co2e=0.09):
    m = Message(
        thread_id=thread.id,
        role=role,
        content="test content",
        tokens_used=tokens,
        energy_wh=energy,
        co2e_g=co2e,
    )
    db.session.add(m)
    db.session.commit()
    return m


class TestArchiveMessageEnergy:
    """archive_message_energy captures energy before deletion."""

    def test_archive_by_thread_id(self, app, db, make_user):
        """Archiving by thread_id captures all assistant message energy."""
        from app.sustainability import archive_message_energy
        with app.app_context():
            db.create_all()
            user = make_user(email="arch@test.com")
            thread = _make_thread(db, user)
            _make_msg(db, thread, tokens=200, energy=1.5, co2e=0.27)
            _make_msg(db, thread, tokens=300, energy=2.0, co2e=0.36)
            _make_msg(db, thread, role="user", tokens=50, energy=None, co2e=None)

            archive_message_energy(thread_id=thread.id, reason="clear")

            ledger = EnergyLedger.query.filter_by(reason="clear").first()
            assert ledger is not None
            assert ledger.message_count == 2  # only assistant messages
            assert ledger.energy_wh == 3.5  # 1.5 + 2.0
            assert ledger.co2e_g == 0.63  # 0.27 + 0.36
            assert ledger.user_id == user.id

    def test_archive_by_message_id(self, app, db, make_user):
        """Archiving by message_id captures single message energy."""
        from app.sustainability import archive_message_energy
        with app.app_context():
            db.create_all()
            user = make_user(email="arch2@test.com")
            thread = _make_thread(db, user)
            msg = _make_msg(db, thread, tokens=500, energy=3.0, co2e=0.54)

            archive_message_energy(message_ids=[msg.id], reason="delete")

            ledger = EnergyLedger.query.filter_by(reason="delete").first()
            assert ledger is not None
            assert ledger.message_count == 1
            assert ledger.energy_wh == 3.0
            assert ledger.co2e_g == 0.54

    def test_archive_empty_thread(self, app, db, make_user):
        """Archiving a thread with no assistant messages creates no ledger entry."""
        from app.sustainability import archive_message_energy
        with app.app_context():
            db.create_all()
            user = make_user(email="arch3@test.com")
            thread = _make_thread(db, user)
            _make_msg(db, thread, role="user", tokens=50, energy=None, co2e=None)

            archive_message_energy(thread_id=thread.id)

            assert EnergyLedger.query.count() == 0

    def test_archive_no_args_does_nothing(self, app, db):
        """Calling with no args is a no-op."""
        from app.sustainability import archive_message_energy
        with app.app_context():
            db.create_all()
            archive_message_energy()
            assert EnergyLedger.query.count() == 0


class TestLedgerTotals:
    """_get_ledger_totals returns archived energy sums."""

    def test_site_wide_totals(self, app, db, make_user):
        from app.sustainability import archive_message_energy, _get_ledger_totals
        with app.app_context():
            db.create_all()
            user = make_user(email="ledger@test.com")
            t1 = _make_thread(db, user)
            _make_msg(db, t1, tokens=100, energy=1.0, co2e=0.18)
            _make_msg(db, t1, tokens=200, energy=2.0, co2e=0.36)
            archive_message_energy(thread_id=t1.id, reason="clear")

            t2 = _make_thread(db, user, title="Thread 2")
            _make_msg(db, t2, tokens=300, energy=3.0, co2e=0.54)
            archive_message_energy(thread_id=t2.id, reason="compact")

            energy, co2e = _get_ledger_totals()
            assert energy == 6.0  # 1+2+3
            assert co2e == 1.08   # 0.18+0.36+0.54

    def test_per_user_totals(self, app, db, make_user):
        from app.sustainability import archive_message_energy, _get_ledger_totals
        with app.app_context():
            db.create_all()
            user1 = make_user(email="u1@test.com")
            user2 = make_user(email="u2@test.com")

            t1 = _make_thread(db, user1)
            _make_msg(db, t1, tokens=100, energy=1.0, co2e=0.18)
            archive_message_energy(thread_id=t1.id)

            t2 = _make_thread(db, user2)
            _make_msg(db, t2, tokens=200, energy=5.0, co2e=0.90)
            archive_message_energy(thread_id=t2.id)

            e1, c1 = _get_ledger_totals(user_id=user1.id)
            assert e1 == 1.0
            assert c1 == 0.18

            e2, c2 = _get_ledger_totals(user_id=user2.id)
            assert e2 == 5.0
            assert c2 == 0.90


class TestSustainabilityIncludesLedger:
    """_get_site_wide_energy and _get_user_energy include ledger totals."""

    def test_site_wide_includes_deleted(self, app, db, make_user):
        from app.sustainability import (
            archive_message_energy, _get_site_wide_energy
        )
        with app.app_context():
            db.create_all()
            user = make_user(email="site@test.com")
            thread = _make_thread(db, user)
            _make_msg(db, thread, tokens=100, energy=2.0, co2e=0.36)
            _make_msg(db, thread, tokens=200, energy=4.0, co2e=0.72)

            # Before deletion
            before = _get_site_wide_energy()

            # Delete and archive
            archive_message_energy(thread_id=thread.id, reason="delete")
            Message.query.filter_by(thread_id=thread.id).delete()
            db.session.commit()

            # After deletion — should be the same (ledger preserves the energy)
            after = _get_site_wide_energy()
            assert after == before

    def test_user_energy_includes_deleted(self, app, db, make_user):
        from app.sustainability import (
            archive_message_energy, _get_user_energy
        )
        with app.app_context():
            db.create_all()
            user = make_user(email="user_energy@test.com")
            thread = _make_thread(db, user)
            _make_msg(db, thread, tokens=100, energy=3.0, co2e=0.54)

            before = _get_user_energy(user.id)

            archive_message_energy(thread_id=thread.id, reason="delete")
            Message.query.filter_by(thread_id=thread.id).delete()
            db.session.commit()

            after = _get_user_energy(user.id)
            assert after == before
