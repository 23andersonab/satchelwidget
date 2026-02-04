# satchelwidget
server hosted python script that gets data via the satchel one api and returns information as a json for use with my Widgy widget


# calling

when calling the satchel one api, you need 3 things:
- Auth/Bearer Token
- User Id
- School Id

You can use some kind of web debugging tool or proxy to find these values. The auth token will likely be in the http headers when opening the satchel one site with your proxy active, and your user & school ids will be in the json response

you can fill these into this api as headers like so:

| Key           | Value         |
| ------------- |:-------------:|
| Authorization | Your auth token (WITHOUT the "Bearer " prefix) |
| X-User-Id     | Your user id      |
| X-School-Id   | Your school id     |
