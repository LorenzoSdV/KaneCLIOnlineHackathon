# One image with all webkit recorders; the compose service picks the binary.
FROM golang:1.27 AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY cmd ./cmd
COPY internal ./internal
RUN CGO_ENABLED=0 go build -trimpath -o /out/ ./cmd/...

FROM alpine:3.22
RUN apk add --no-cache ca-certificates
COPY --from=build /out/ /usr/local/bin/
