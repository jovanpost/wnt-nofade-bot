CREATE TABLE activity (
	id SERIAL NOT NULL,
	ts TIMESTAMP WITH TIME ZONE,
	level VARCHAR(16),
	kind VARCHAR(48),
	message TEXT,
	PRIMARY KEY (id)
);
CREATE INDEX ix_activity_ts ON activity (ts);

CREATE TABLE bot_state (
	key VARCHAR(64) NOT NULL,
	value TEXT,
	updated_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (key)
);

CREATE TABLE days (
	event_date VARCHAR(10) NOT NULL,
	event_ticker VARCHAR(64),
	detected_at TIMESTAMP WITH TIME ZONE,
	markets_seen INTEGER,
	orders_placed INTEGER,
	orders_rejected INTEGER,
	collateral FLOAT,
	cancelled_at TIMESTAMP WITH TIME ZONE,
	cancel_verified BOOLEAN,
	mode VARCHAR(32),
	notes TEXT,
	PRIMARY KEY (event_date)
);

CREATE TABLE depth (
	id SERIAL NOT NULL,
	ts TIMESTAMP WITH TIME ZONE,
	event_date VARCHAR(10),
	market_ticker VARCHAR(128),
	our_no_cents INTEGER,
	best_yes_bid INTEGER,
	best_no_bid INTEGER,
	yes_size_total FLOAT,
	no_size_total FLOAT,
	no_size_ahead FLOAT,
	no_size_at_our_price FLOAT,
	yes_size_that_would_fill_us FLOAT,
	yes_book JSON,
	no_book JSON,
	PRIMARY KEY (id)
);
CREATE INDEX ix_depth_event_date ON depth (event_date);
CREATE INDEX ix_depth_ts ON depth (ts);
CREATE INDEX ix_depth_market_ticker ON depth (market_ticker);

CREATE TABLE fills (
	id SERIAL NOT NULL,
	fill_id VARCHAR(64),
	order_id VARCHAR(64),
	event_date VARCHAR(10),
	market_ticker VARCHAR(128),
	contracts FLOAT,
	price_cents INTEGER,
	is_taker BOOLEAN,
	fee_cents FLOAT,
	created_at TIMESTAMP WITH TIME ZONE,
	raw JSON,
	PRIMARY KEY (id)
);
CREATE INDEX ix_fills_order_id ON fills (order_id);
CREATE UNIQUE INDEX ix_fills_fill_id ON fills (fill_id);
CREATE INDEX ix_fills_event_date ON fills (event_date);

CREATE TABLE orders (
	id SERIAL NOT NULL,
	client_order_id VARCHAR(64),
	event_date VARCHAR(10),
	event_ticker VARCHAR(64),
	market_ticker VARCHAR(128),
	title VARCHAR(255),
	no_price_cents INTEGER,
	yes_price_cents INTEGER,
	contracts INTEGER,
	collateral FLOAT,
	placed_at TIMESTAMP WITH TIME ZONE,
	order_id VARCHAR(64),
	dry_run BOOLEAN,
	mode VARCHAR(32),
	yes_bid_at_place INTEGER,
	yes_ask_at_place INTEGER,
	no_bid_at_place INTEGER,
	no_ask_at_place INTEGER,
	book_at_place JSON,
	status VARCHAR(24),
	reject_reason TEXT,
	filled_contracts FLOAT,
	first_fill_at TIMESTAMP WITH TIME ZONE,
	avg_fill_price_cents FLOAT,
	fees_cents FLOAT,
	cancelled_at TIMESTAMP WITH TIME ZONE,
	expiration_epoch INTEGER,
	result VARCHAR(8),
	realized_pnl FLOAT,
	PRIMARY KEY (id)
);
CREATE INDEX ix_orders_market_ticker ON orders (market_ticker);
CREATE INDEX ix_orders_event_date ON orders (event_date);
CREATE UNIQUE INDEX ix_orders_client_order_id ON orders (client_order_id);
