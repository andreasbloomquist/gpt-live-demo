---
id: skills/restaurant_reservations.backend
version: 1
target: backend
description: Tool policy for check_restaurant_availability
requires_tools: [check_restaurant_availability]
variables: [city_hint]
---
# Restaurant availability

Use `check_restaurant_availability` to see whether a restaurant has a table. It only checks
availability; it cannot book, hold, change, or cancel a reservation, and nothing else you have
access to can either.

Arguments:
- `restaurant`: the restaurant name as the caller said it, with obvious speech-recognition
  errors corrected only if you are confident.
- `date`: ISO YYYY-MM-DD, resolved from relative phrases using today's date.
- `time`: 24-hour HH:MM.
- `party_size`: a whole number of people, including the caller.
- `city`: pass it when the caller mentions one; otherwise omit it (the default area is
  {{ city_hint }}).

Required before calling: restaurant, date, time, and party size. If any is missing, ask for it
instead of calling the tool.

Reporting results:
- If the exact time is available, say so, and add that the caller can finish booking with the
  restaurant or in the app.
- Otherwise offer at most three nearby times, closest first.
- If the restaurant is closed that day, or nothing is available, say so and suggest trying
  another day, time, or restaurant.
- If the tool returns an error, relay its meaning in plain words and suggest a next step.
- Never state or imply that a table has been reserved, held, or confirmed.
