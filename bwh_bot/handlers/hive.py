import frappe
from telegram import InlineKeyboardButton

from bwh_bot.conversation import BotConversation
from bwh_bot.telegram_utils import answer_callback_query, edit_message_text, send_message
from bwh_bot.ui import (
	confirm_buttons,
	from_date_buttons,
	make_keyboard,
	nav_buttons,
	to_date_buttons,
)

# Status a Telegram-created task lands in. Every other status in Hive Task is a
# valid transition target from this one.
DEFAULT_STATUS = "To Do"

# Cap the pickers so a long list does not build an unwieldy inline keyboard.
# Telegram silently rejects very large keyboards, so these are deliberate.
MAX_PROJECTS = 30
MAX_MEMBERS = 30


class HiveTaskConversation(BotConversation):
	handler_name = "hive"
	callback_prefix = "hive"
	command = "/hive"
	command_description = "Create a task in Hive"
	title = "New Hive Task"

	# Hive Task has no HR dependency, so this flow runs on sites without Frappe HR.
	requires_employee = False
	required_doctypes = ("Hive Project", "Hive Task", "Hive Member")

	# --- Step 1: title -------------------------------------------------------

	def on_start(self, message, state, employee):
		chat_id = message["chat"]["id"]
		message_thread_id = message.get("message_thread_id")

		self.update_state(state, "awaiting_title")
		send_message(
			chat_id,
			f"<b>{self.title}</b>\n\nReply with the <b>task title</b>:",
			parse_mode="HTML",
			reply_to_message_id=message["message_id"],
			message_thread_id=message_thread_id,
		)

	# --- Free-text steps: title, description ---------------------------------

	def on_raw_text_input(self, state, chat_id, text):
		message_thread_id = self.get_data(state).get("message_thread_id")

		if state.step == "awaiting_title":
			title = text.strip()
			if not title:
				send_message(
					chat_id,
					"Task title can't be empty. Reply with the <b>task title</b>:",
					parse_mode="HTML",
					message_thread_id=message_thread_id,
				)
				return

			self.update_state(state, "awaiting_description", {"title": title})
			self._prompt_description(state, chat_id, message_thread_id=message_thread_id)

		elif state.step == "awaiting_description":
			self.update_state(state, "select_start_date", {"description": text.strip()})
			self._prompt_start_date(state, chat_id, message_thread_id=message_thread_id)

	# --- Date steps ----------------------------------------------------------
	# Quick-pick buttons emit `from`/`to` actions (see on_action). The base class
	# routes the "Custom date..." buttons through awaiting_from_date /
	# awaiting_to_date and hands the parsed date to on_text_input.

	def on_text_input(self, state, chat_id, date_str):
		message_thread_id = self.get_data(state).get("message_thread_id")

		if state.step == "awaiting_from_date":
			self.update_state(state, "select_end_date", {"start_date": date_str})
			self._prompt_end_date(state, chat_id, message_thread_id=message_thread_id)

		elif state.step == "awaiting_to_date":
			if not self._reject_end_before_start(state, chat_id, date_str, message_thread_id):
				return
			self.update_state(state, "select_project", {"due_date": date_str})
			self._prompt_project(state, chat_id, message_thread_id=message_thread_id)

	# --- Button actions ------------------------------------------------------

	def on_action(self, action, value, state, ctx):
		chat_id, message_id, cqid = ctx["chat_id"], ctx["message_id"], ctx["callback_query_id"]

		if action == "skip_desc":
			self.update_state(state, "select_start_date", {"description": ""})
			answer_callback_query(cqid)
			self._prompt_start_date(state, chat_id, message_id=message_id)

		elif action == "from":
			self.update_state(state, "select_end_date", {"start_date": value})
			answer_callback_query(cqid)
			self._prompt_end_date(state, chat_id, message_id=message_id)

		elif action == "to":
			if not self._reject_end_before_start(state, chat_id, value, None, cqid=cqid):
				return
			self.update_state(state, "select_project", {"due_date": value})
			answer_callback_query(cqid)
			self._prompt_project(state, chat_id, message_id=message_id)

		elif action == "project":
			title = frappe.db.get_value("Hive Project", value, "title") or value
			self.update_state(state, "select_assignees", {"project": value, "project_title": title})
			answer_callback_query(cqid)
			self._prompt_assignees(state, chat_id, message_id=message_id)

		elif action == "assignee":
			self._toggle_assignee(state, value)
			answer_callback_query(cqid)
			self._prompt_assignees(state, chat_id, message_id=message_id)

		elif action == "assignees_done":
			self.update_state(state, "confirm")
			answer_callback_query(cqid)
			self._show_summary(state, chat_id, message_id)

		elif action == "confirm":
			self._create_task(state, ctx)

		else:
			answer_callback_query(cqid, "Unknown action.")

	def on_back(self, state, ctx):
		chat_id, message_id = ctx["chat_id"], ctx["message_id"]
		step = state.step

		if step == "select_start_date":
			self.update_state(state, "awaiting_description")
			self._prompt_description(state, chat_id, message_id=message_id)

		elif step == "select_end_date":
			self.update_state(state, "select_start_date")
			self._prompt_start_date(state, chat_id, message_id=message_id)

		elif step == "select_project":
			self.update_state(state, "select_end_date")
			self._prompt_end_date(state, chat_id, message_id=message_id)

		elif step == "select_assignees":
			self.update_state(state, "select_project")
			self._prompt_project(state, chat_id, message_id=message_id)

		elif step == "confirm":
			self.update_state(state, "select_assignees")
			self._prompt_assignees(state, chat_id, message_id=message_id)

	# --- Prompts -------------------------------------------------------------

	def _prompt_description(self, state, chat_id, message_id=None, message_thread_id=None):
		self._send(
			chat_id,
			f"{self._build_header(self.get_data(state))}\n\nReply with a <b>description</b>, or tap Skip:",
			make_keyboard(
				[
					[
						InlineKeyboardButton(
							"Skip (no description)", callback_data=f"{self.callback_prefix}:skip_desc"
						)
					]
				],
				nav_buttons(self.callback_prefix, show_back=False),
			),
			message_id=message_id,
			message_thread_id=message_thread_id,
		)

	def _prompt_start_date(self, state, chat_id, message_id=None, message_thread_id=None):
		self._send(
			chat_id,
			f"{self._build_header(self.get_data(state))}\n\nSelect <b>start date</b>:",
			make_keyboard(
				from_date_buttons(self.callback_prefix, include_today=True),
				nav_buttons(self.callback_prefix),
			),
			message_id=message_id,
			message_thread_id=message_thread_id,
		)

	def _prompt_end_date(self, state, chat_id, message_id=None, message_thread_id=None):
		data = self.get_data(state)
		self._send(
			chat_id,
			f"{self._build_header(data)}\n\nSelect <b>end date</b>:",
			make_keyboard(
				to_date_buttons(self.callback_prefix, data["start_date"]),
				nav_buttons(self.callback_prefix),
			),
			message_id=message_id,
			message_thread_id=message_thread_id,
		)

	def _prompt_project(self, state, chat_id, message_id=None, message_thread_id=None):
		projects = frappe.get_all(
			"Hive Project",
			filters={"is_archived": 0, "status": "Open"},
			fields=["name", "title"],
			order_by="modified desc",
			limit=MAX_PROJECTS,
		)
		if not projects:
			self._abort(
				state,
				chat_id,
				"No open projects found in Hive. Create a project first.",
				message_id,
				message_thread_id,
			)
			return

		self._send(
			chat_id,
			f"{self._build_header(self.get_data(state))}\n\nSelect <b>project</b>:",
			make_keyboard(
				_two_per_row(
					[(p.title or p.name, f"{self.callback_prefix}:project:{p.name}") for p in projects]
				),
				nav_buttons(self.callback_prefix),
			),
			message_id=message_id,
			message_thread_id=message_thread_id,
		)

	def _prompt_assignees(self, state, chat_id, message_id=None, message_thread_id=None):
		"""Multi-select picker: each tap toggles a member, Done moves on."""
		members = _active_members(MAX_MEMBERS)
		if not members:
			self._abort(
				state,
				chat_id,
				"No active Hive members found to assign this task to.",
				message_id,
				message_thread_id,
			)
			return

		selected = set(self.get_data(state).get("assignees") or [])
		rows = _two_per_row(
			[
				(
					f"{'✅ ' if m.user in selected else ''}{m.member_name or m.user}",
					f"{self.callback_prefix}:assignee:{m.user}",
				)
				for m in members
			]
		)
		done_label = f"Done ({len(selected)} selected)" if selected else "Done (no assignees)"
		rows.append(
			[InlineKeyboardButton(done_label, callback_data=f"{self.callback_prefix}:assignees_done")]
		)

		self._send(
			chat_id,
			f"{self._build_header(self.get_data(state))}\n\nSelect <b>assignees</b> (tap to toggle):",
			make_keyboard(rows, nav_buttons(self.callback_prefix)),
			message_id=message_id,
			message_thread_id=message_thread_id,
		)

	def _show_summary(self, state, chat_id, message_id):
		data = self.get_data(state)
		assignees = data.get("assignees") or []
		description = data.get("description") or ""

		self._send(
			chat_id,
			(
				f"<b>{self.title} — Review</b>\n\n"
				f"<b>Title:</b> {frappe.utils.escape_html(data['title'])}\n"
				f"<b>Description:</b> {frappe.utils.escape_html(description) if description else '—'}\n"
				f"<b>Project:</b> {frappe.utils.escape_html(data['project_title'])}\n"
				f"<b>Start:</b> {data['start_date']}\n"
				f"<b>End:</b> {data['due_date']}\n"
				f"<b>Assignees:</b> {', '.join(assignees) if assignees else '—'}\n\n"
				"Create this task?"
			),
			make_keyboard(confirm_buttons(self.callback_prefix, "Confirm & Create")),
			message_id=message_id,
		)

	# --- Creation ------------------------------------------------------------

	def _create_task(self, state, ctx):
		data = self.get_data(state)
		self.clear_state(state)
		chat_id, message_id, cqid = ctx["chat_id"], ctx["message_id"], ctx["callback_query_id"]
		assignees = data.get("assignees") or []

		try:
			frappe.db.savepoint("before_hive_task")
			doc = frappe.get_doc(
				{
					"doctype": "Hive Task",
					"title": data["title"],
					"description": data.get("description") or None,
					"project": data["project"],
					"status": DEFAULT_STATUS,
					"start_date": data["start_date"],
					"due_date": data["due_date"],
				}
			)
			doc.insert()
		except Exception as e:
			frappe.db.rollback(save_point="before_hive_task")
			answer_callback_query(cqid, "Failed to create task.", show_alert=True)
			edit_message_text(chat_id, message_id, f"Failed to create task: {e}")
			return

		# Assignment is best-effort: a task with no assignees is still useful, so
		# report the task as created even if assigning trips a permission issue,
		# rather than losing the whole task.
		assign_note = ""
		if assignees:
			try:
				from frappe.desk.form.assign_to import add as assign_add

				assign_add(
					{
						"doctype": "Hive Task",
						"name": doc.name,
						"assign_to": assignees,
						"notify": 0,
					}
				)
			except Exception:
				frappe.log_error(title="hive: assign failed", message=f"Assign {assignees} to {doc.name}")
				assign_note = "\n⚠️ Task created, but assigning failed — assign manually in Hive."

		# The task is created and assignment has been attempted, so persist before
		# talking to Telegram: a failure in the reply must not roll the task back.
		frappe.db.commit()  # nosemgrep

		answer_callback_query(cqid, "Task created!")
		edit_message_text(
			chat_id,
			message_id,
			(
				f"<b>✅ Hive Task Created</b>\n\n"
				f"<b>ID:</b> {doc.name}\n"
				f"<b>Title:</b> {frappe.utils.escape_html(doc.title)}\n"
				f"<b>Project:</b> {frappe.utils.escape_html(data['project_title'])}\n"
				f"<b>Start:</b> {data['start_date']}\n"
				f"<b>End:</b> {data['due_date']}\n"
				f"<b>Status:</b> {DEFAULT_STATUS}\n"
				f"<b>Assignees:</b> {', '.join(assignees) if assignees else '—'}"
				f"{assign_note}"
			),
			parse_mode="HTML",
		)

	# --- Helpers -------------------------------------------------------------

	def _send(self, chat_id, text, reply_markup, message_id=None, message_thread_id=None):
		"""Edit in place when reacting to a button, otherwise post a new message."""
		if message_id:
			edit_message_text(chat_id, message_id, text, parse_mode="HTML", reply_markup=reply_markup)
		else:
			send_message(
				chat_id,
				text,
				parse_mode="HTML",
				reply_markup=reply_markup,
				message_thread_id=message_thread_id,
			)

	def _abort(self, state, chat_id, text, message_id, message_thread_id):
		self.clear_state(state)
		if message_id:
			edit_message_text(chat_id, message_id, text)
		else:
			send_message(chat_id, text, message_thread_id=message_thread_id)

	def _toggle_assignee(self, state, user):
		selected = self.get_data(state).get("assignees") or []
		if user in selected:
			selected.remove(user)
		else:
			selected.append(user)
		self.update_state(state, state.step, {"assignees": selected})

	def _reject_end_before_start(self, state, chat_id, end_date, message_thread_id, cqid=None):
		"""Hive treats due_date as on/after start_date; reject anything earlier."""
		start = self.get_data(state).get("start_date")
		if start and frappe.utils.getdate(end_date) < frappe.utils.getdate(start):
			msg = f"End date ({end_date}) can't be before the start date ({start})."
			if cqid:
				answer_callback_query(cqid, msg, show_alert=True)
			else:
				send_message(chat_id, msg, message_thread_id=message_thread_id)
			return False
		return True

	def _build_header(self, data):
		parts = [f"<b>{self.title}</b>"]
		if data.get("title"):
			parts.append(f"<b>Title:</b> {frappe.utils.escape_html(data['title'])}")
		if data.get("description"):
			parts.append(f"<b>Description:</b> {frappe.utils.escape_html(data['description'])}")
		if data.get("start_date"):
			parts.append(f"<b>Start:</b> {data['start_date']}")
		if data.get("due_date"):
			parts.append(f"<b>End:</b> {data['due_date']}")
		if data.get("project_title"):
			parts.append(f"<b>Project:</b> {frappe.utils.escape_html(data['project_title'])}")
		return "\n".join(parts)


def _active_members(limit):
	"""Team members who can be assigned work, newest-first."""
	return frappe.get_all(
		"Hive Member",
		filters={"is_active": 1, "type": "Team"},
		fields=["user", "member_name"],
		order_by="member_name asc",
		limit=limit,
	)


def _two_per_row(entries):
	"""Lay out (label, callback_data) pairs two to a row."""
	rows, row = [], []
	for label, callback_data in entries:
		row.append(InlineKeyboardButton(label, callback_data=callback_data))
		if len(row) == 2:
			rows.append(row)
			row = []
	if row:
		rows.append(row)
	return rows
