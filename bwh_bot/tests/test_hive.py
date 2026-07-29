# Copyright (c) 2026, BWH Studios and Contributors
# See license.txt
"""Tests for the /hive conversation.

Telegram transport is patched out throughout — these cover the conversation
state machine and the Hive Task it produces, not the HTTP round trip.
"""

from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from bwh_bot.handlers.hive import DEFAULT_STATUS, HiveTaskConversation

CHAT_ID = 987001
TG_USER_ID = 424242


def _make_project(title="Bot Test Project", **kwargs):
	return frappe.get_doc({"doctype": "Hive Project", "title": title, "status": "Open", **kwargs}).insert(
		ignore_permissions=True
	)


def _make_member(user, member_name=None, **kwargs):
	return frappe.get_doc(
		{
			"doctype": "Hive Member",
			"user": user,
			"member_name": member_name or user,
			"type": "Team",
			"is_active": 1,
			**kwargs,
		}
	).insert(ignore_permissions=True)


def _command_message(chat_id=CHAT_ID, user_id=TG_USER_ID):
	return {"chat": {"id": chat_id}, "message_id": 1, "from": {"id": user_id}}


class TestHiveConversation(IntegrationTestCase):
	"""Drives the flow end to end with the Telegram calls patched out."""

	def setUp(self):
		# Hive is an optional integration: bwh_bot installs without it, and CI
		# runs on a site that has no Hive doctypes.
		if not frappe.db.exists("DocType", "Hive Task"):
			self.skipTest("Hive (bwh_hive) is not installed on this site")

		self.project = _make_project()
		if not frappe.db.exists("Hive Member", {"user": "Administrator"}):
			_make_member("Administrator")

		# Each test starts from a clean session for this chat.
		for name in frappe.get_all(
			"Telegram Conversation State",
			filters={"chat_id": str(CHAT_ID), "handler": "hive"},
			pluck="name",
		):
			frappe.delete_doc("Telegram Conversation State", name, force=True, ignore_permissions=True)

		self.handler = HiveTaskConversation()
		self.ctx = {"chat_id": CHAT_ID, "message_id": 1, "callback_query_id": "cq1"}

		patcher = patch.multiple(
			"bwh_bot.handlers.hive",
			send_message=self._record,
			edit_message_text=self._record_edit,
			answer_callback_query=self._record_answer,
		)
		patcher.start()
		self.addCleanup(patcher.stop)

		conv_patcher = patch.multiple(
			"bwh_bot.conversation",
			send_message=self._record,
			edit_message_text=self._record_edit,
			answer_callback_query=self._record_answer,
			set_message_reaction=lambda *a, **k: None,
		)
		conv_patcher.start()
		self.addCleanup(conv_patcher.stop)

		self.sent = []
		self.markups = []

	# --- capture helpers ---

	def _record(self, chat_id, text, **kwargs):
		self.sent.append(text)
		self.markups.append(kwargs.get("reply_markup"))

	def _record_edit(self, chat_id, message_id, text, **kwargs):
		self.sent.append(text)
		self.markups.append(kwargs.get("reply_markup"))

	def _record_answer(self, callback_query_id, text=None, show_alert=False):
		self.sent.append(text or "")
		self.markups.append(None)

	@property
	def last(self):
		return self.sent[-1]

	@property
	def last_buttons(self):
		"""Labels of the most recent inline keyboard, flattened."""
		markup = self.markups[-1]
		if not markup:
			return []
		return [button.text for row in markup.inline_keyboard for button in row]

	# --- flow driver ---

	def _run_to_confirm(self, description="A description", assignees=("Administrator",)):
		"""Walk the conversation up to (not including) the confirm action."""
		self.handler.handle_command(_command_message())
		state = self.handler.get_active_state(CHAT_ID, TG_USER_ID)

		self.handler.handle_text_input(state, CHAT_ID, "Test task title")
		if description is None:
			self.handler.on_action("skip_desc", None, state, self.ctx)
		else:
			self.handler.handle_text_input(state, CHAT_ID, description)

		self.handler.on_action("from", "2026-08-03", state, self.ctx)
		self.handler.on_action("to", "2026-08-07", state, self.ctx)
		self.handler.on_action("project", self.project.name, state, self.ctx)

		for user in assignees:
			self.handler.on_action("assignee", user, state, self.ctx)
		self.handler.on_action("assignees_done", None, state, self.ctx)
		return state

	# --- tests ---

	def test_creates_task_with_all_fields(self):
		state = self._run_to_confirm()
		self.handler.on_action("confirm", None, state, self.ctx)

		task = frappe.get_last_doc("Hive Task")
		self.assertEqual(task.title, "Test task title")
		self.assertIn("A description", task.description)
		self.assertEqual(task.project, self.project.name)
		self.assertEqual(task.status, DEFAULT_STATUS)
		self.assertEqual(str(task.start_date), "2026-08-03")
		self.assertEqual(str(task.due_date), "2026-08-07")
		self.assertIn("Hive Task Created", self.last)

	def test_assignees_land_in_assign(self):
		state = self._run_to_confirm()
		self.handler.on_action("confirm", None, state, self.ctx)

		task = frappe.get_last_doc("Hive Task")
		self.assertIn("Administrator", frappe.db.get_value("Hive Task", task.name, "_assign") or "")

	def test_description_can_be_skipped(self):
		state = self._run_to_confirm(description=None)
		self.handler.on_action("confirm", None, state, self.ctx)

		task = frappe.get_last_doc("Hive Task")
		self.assertFalse(task.description)

	def test_task_can_be_created_without_assignees(self):
		state = self._run_to_confirm(assignees=())
		self.handler.on_action("confirm", None, state, self.ctx)

		task = frappe.get_last_doc("Hive Task")
		self.assertFalse(frappe.db.get_value("Hive Task", task.name, "_assign"))

	def test_assignee_selection_toggles_off(self):
		self.handler.handle_command(_command_message())
		state = self.handler.get_active_state(CHAT_ID, TG_USER_ID)
		self.handler.handle_text_input(state, CHAT_ID, "Toggle test")
		self.handler.on_action("skip_desc", None, state, self.ctx)
		self.handler.on_action("from", "2026-08-03", state, self.ctx)
		self.handler.on_action("to", "2026-08-07", state, self.ctx)
		self.handler.on_action("project", self.project.name, state, self.ctx)

		self.handler.on_action("assignee", "Administrator", state, self.ctx)
		self.assertEqual(self.handler.get_data(state)["assignees"], ["Administrator"])
		self.assertIn("✅ Administrator", self.last_buttons)
		self.assertIn("Done (1 selected)", self.last_buttons)

		self.handler.on_action("assignee", "Administrator", state, self.ctx)
		self.assertEqual(self.handler.get_data(state)["assignees"], [])
		self.assertNotIn("✅ Administrator", self.last_buttons)

	def test_end_date_before_start_is_rejected(self):
		self.handler.handle_command(_command_message())
		state = self.handler.get_active_state(CHAT_ID, TG_USER_ID)
		self.handler.handle_text_input(state, CHAT_ID, "Bad dates")
		self.handler.on_action("skip_desc", None, state, self.ctx)
		self.handler.on_action("from", "2026-08-10", state, self.ctx)

		self.handler.on_action("to", "2026-08-01", state, self.ctx)

		self.assertIn("can't be before the start date", self.last)
		# Rejected, so the flow has not advanced past date selection.
		self.assertEqual(state.step, "select_end_date")

	def test_empty_title_is_rejected(self):
		self.handler.handle_command(_command_message())
		state = self.handler.get_active_state(CHAT_ID, TG_USER_ID)

		self.handler.handle_text_input(state, CHAT_ID, "   ")

		self.assertIn("can't be empty", self.last)
		self.assertEqual(state.step, "awaiting_title")

	def test_back_from_project_returns_to_end_date(self):
		self.handler.handle_command(_command_message())
		state = self.handler.get_active_state(CHAT_ID, TG_USER_ID)
		self.handler.handle_text_input(state, CHAT_ID, "Back test")
		self.handler.on_action("skip_desc", None, state, self.ctx)
		self.handler.on_action("from", "2026-08-03", state, self.ctx)
		self.handler.on_action("to", "2026-08-07", state, self.ctx)
		self.assertEqual(state.step, "select_project")

		self.handler.on_back(state, self.ctx)

		self.assertEqual(state.step, "select_end_date")
		self.assertIn("end date", self.last)

	def test_archived_projects_are_not_offered(self):
		archived = _make_project(title="Archived Project", is_archived=1)
		self.handler.handle_command(_command_message())
		state = self.handler.get_active_state(CHAT_ID, TG_USER_ID)
		self.handler.handle_text_input(state, CHAT_ID, "Project filter test")
		self.handler.on_action("skip_desc", None, state, self.ctx)
		self.handler.on_action("from", "2026-08-03", state, self.ctx)
		self.handler.on_action("to", "2026-08-07", state, self.ctx)

		self.assertIn(self.project.title, self.last_buttons)
		self.assertNotIn(archived.title, self.last_buttons)


class TestConversationRequirements(IntegrationTestCase):
	"""The dependency gate that keeps commands usable on partially-installed sites."""

	def test_hive_does_not_require_employee(self):
		self.assertFalse(HiveTaskConversation.requires_employee)

	def test_hive_declares_its_hive_doctypes(self):
		self.assertIn("Hive Task", HiveTaskConversation.required_doctypes)

	def test_missing_doctypes_reported_when_absent(self):
		handler = HiveTaskConversation()
		with patch.object(HiveTaskConversation, "required_doctypes", ("No Such Doctype",)):
			self.assertEqual(handler.missing_doctypes(), ["No Such Doctype"])

	def test_nothing_missing_when_every_required_doctype_exists(self):
		handler = HiveTaskConversation()
		with patch.object(HiveTaskConversation, "required_doctypes", ("User",)):
			self.assertEqual(handler.missing_doctypes(), [])

	def test_only_absent_doctypes_are_reported(self):
		handler = HiveTaskConversation()
		with patch.object(HiveTaskConversation, "required_doctypes", ("User", "No Such Doctype")):
			self.assertEqual(handler.missing_doctypes(), ["No Such Doctype"])

	def test_each_command_keeps_its_own_description(self):
		"""Descriptions must not be shared: every conversation registers the same
		bound BotConversation.handle_command, so storing them on the function
		object made the last registration overwrite all the others."""
		import bwh_bot.api.telegram as telegram_api

		descriptions = telegram_api.COMMAND_DESCRIPTIONS
		self.assertEqual(descriptions.get("/hive"), "Create a task in Hive")
		self.assertEqual(descriptions.get("/leave_application"), "Apply for leave")
		self.assertEqual(descriptions.get("/wfh"), "Apply for Work From Home")
		self.assertEqual(len(set(descriptions.values())), len(descriptions))

	def test_requires_employee_adds_employee_to_the_check(self):
		"""requires_employee implies Frappe HR, so Employee joins the required set."""
		handler = HiveTaskConversation()
		with patch.object(HiveTaskConversation, "required_doctypes", ()):
			with patch.object(HiveTaskConversation, "requires_employee", True):
				expected = [] if frappe.db.exists("DocType", "Employee") else ["Employee"]
				self.assertEqual(handler.missing_doctypes(), expected)

			with patch.object(HiveTaskConversation, "requires_employee", False):
				self.assertEqual(handler.missing_doctypes(), [])
