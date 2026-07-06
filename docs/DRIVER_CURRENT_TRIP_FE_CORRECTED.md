# Corrected driver CurrentTrip frontend

This is the complete corrected `CurrentTrip.tsx` reference implementation.

Key behavior:

- Trip-start authorization comes only from `trip.start_allowed`; there is no client countdown or timer-derived permission.
- Departure authorization comes only from the matching `trip.departure_allowed` event.
- Arrival HTTP completion never overwrites a newer WebSocket eligibility event.
- The same stop-specific eligibility function controls rendering, disabled state, and click validation.
- Every `channel.connected` message completes synchronization, including reconnects.
- WebSocket callbacks capture the current token and read the latest trip through a ref.

## Full code

```tsx
import React, { useEffect, useState, useCallback, useRef } from "react";
import { IonPage, IonContent, IonLoading, IonToast } from "@ionic/react";
import { Preferences } from '@capacitor/preferences';
import NavbarSidebar from "./Navbar";
import { 
  FiCamera, 
  FiCheckCircle, 
  FiArrowRightCircle, 
  FiX, 
  FiAlertCircle,
  FiMapPin,
  FiClock,
  FiCalendar,
  FiTruck,
  FiUserCheck,
  FiNavigation,
  FiStopCircle,
  FiFlag,
  FiPlay,
  FiSquare,
  FiCompass,
  FiTarget,
  FiBell,
  FiKey,
  FiUserPlus,
  FiUsers,
  FiUser,
  FiDollarSign,
  FiDownload,
  FiGrid,
  FiPhone,
  FiMail,
  FiWifi,
  FiWifiOff,
} from "react-icons/fi";
import QRScannerComponent from "../pages/ScannerComponent";

const API_BASE = "https://be.shuttleapp.transev.site";

// ============================================================
// WebSocket Types
// ============================================================

export type DriverRefreshEvent =
  | "channel.connected"
  | "route.created"
  | "route.updated"
  | "trip.created"
  | "trip.start_allowed"
  | "trip.started"
  | "trip.stop_arrived"
  | "trip.departure_allowed"
  | "trip.stop_departed"
  | "trip.completed"
  | "trip.cancelled"
  | "trip.premature_ended"
  | "booking.changed"
  | "passenger.scan_completed"
  | "rfid.scan_completed";

export interface DriverRefreshMessage {
  type: "api.refresh";
  event: DriverRefreshEvent;
  audience: "driver";
  resources: string[];
  endpoints: string[];
  data: Record<string, unknown>;
  occurred_at: string;
}

type ConnectionState = "connecting" | "connected" | "disconnected";

interface DriverActionState {
  socketSynchronized: boolean;
  start: {
    tripId: string;
    allowed: boolean;
    windowEndsAt: string | null;
  } | null;
  departure: {
    tripId: string;
    stopId: string;
    sequenceNo: number | null;
    allowed: boolean;
  } | null;
}

// ============================================================
// WebSocket Client Factory
// ============================================================

interface DriverRefreshSocketOptions {
  apiBaseUrl: string;
  accessToken: string;
  onEvent: (event: DriverRefreshMessage) => void;
  onRefreshBatch: (events: DriverRefreshMessage[]) => void;
  onConnectionState?: (state: ConnectionState) => void;
  onAuthenticationFailure?: () => void;
  onProtocolError?: (value: unknown) => void;
}

function createDriverRefreshSocket(options: DriverRefreshSocketOptions) {
  const retryDelays = [1000, 2000, 5000, 10000, 30000];
  let token = options.accessToken;
  let socket: WebSocket | null = null;
  let retryAttempt = 0;
  let retryTimer: ReturnType<typeof setTimeout> | null = null;
  let batchTimer: ReturnType<typeof setTimeout> | null = null;
  let pendingEvents: DriverRefreshMessage[] = [];
  let stopped = true;
  let generation = 0;

  const buildUrl = () => {
    const url = new URL(options.apiBaseUrl);
    url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
    url.pathname = `${url.pathname.replace(/\/$/, "")}/driver/ws/refresh`;
    url.search = "";
    url.searchParams.set("token", token);
    return url.toString();
  };

  const flushBatch = () => {
    batchTimer = null;
    const batch = pendingEvents;
    pendingEvents = [];
    if (batch.length > 0) options.onRefreshBatch(batch);
  };

  const enqueue = (event: DriverRefreshMessage) => {
    options.onEvent(event);
    pendingEvents.push(event);
    if (batchTimer === null) {
      batchTimer = setTimeout(flushBatch, 100);
    }
  };

  const isRefreshMessage = (value: unknown): value is DriverRefreshMessage => {
    if (typeof value !== "object" || value === null) return false;
    const item = value as Record<string, unknown>;
    return (
      item.type === "api.refresh" &&
      item.audience === "driver" &&
      typeof item.event === "string" &&
      Array.isArray(item.resources) &&
      Array.isArray(item.endpoints) &&
      typeof item.data === "object" &&
      item.data !== null &&
      typeof item.occurred_at === "string"
    );
  };

  const scheduleReconnect = () => {
    if (stopped || retryTimer !== null || !navigator.onLine) return;
    const base = retryDelays[Math.min(retryAttempt, retryDelays.length - 1)];
    retryAttempt += 1;
    const delay = Math.round(base * (0.8 + Math.random() * 0.4));
    retryTimer = setTimeout(() => {
      retryTimer = null;
      connect();
    }, delay);
  };

  function connect() {
    if (stopped || !token || !navigator.onLine) return;
    if (socket?.readyState === WebSocket.OPEN || socket?.readyState === WebSocket.CONNECTING) return;

    const myGeneration = generation;
    options.onConnectionState?.("connecting");

    const ws = new WebSocket(buildUrl());
    socket = ws;

    ws.onopen = () => {
      if (myGeneration !== generation) return;
      retryAttempt = 0;
      options.onConnectionState?.("connected");
    };

    ws.onmessage = ({ data }) => {
      if (myGeneration !== generation) return;
      let message: unknown;
      try {
        message = JSON.parse(String(data));
      } catch {
        options.onProtocolError?.(data);
        return;
      }

      if (
        typeof message === "object" &&
        message !== null &&
        (message as { type?: unknown }).type === "ping"
      ) {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "pong" }));
        }
        return;
      }

      if (
        typeof message === "object" &&
        message !== null &&
        (message as { type?: unknown }).type === "ws.ready"
      ) {
        return;
      }

      if (isRefreshMessage(message)) {
        enqueue(message);
      } else {
        options.onProtocolError?.(message);
      }
    };

    ws.onclose = ({ code }) => {
      if (myGeneration !== generation) return;
      socket = null;
      options.onConnectionState?.("disconnected");
      if (stopped) return;
      if (code === 1008) {
        options.onAuthenticationFailure?.();
        return;
      }
      scheduleReconnect();
    };
  }

  const onOnline = () => {
    if (!stopped && socket?.readyState !== WebSocket.OPEN) connect();
  };

  return {
    start() {
      if (!stopped) return;
      stopped = false;
      window.addEventListener("online", onOnline);
      connect();
    },
    stop() {
      stopped = true;
      generation += 1;
      window.removeEventListener("online", onOnline);
      if (retryTimer !== null) clearTimeout(retryTimer);
      if (batchTimer !== null) clearTimeout(batchTimer);
      retryTimer = null;
      batchTimer = null;
      pendingEvents = [];
      socket?.close(1000, "client shutdown");
      socket = null;
      options.onConnectionState?.("disconnected");
    },
    replaceAccessToken(nextToken: string) {
      token = nextToken;
      generation += 1;
      socket?.close(1000, "token replaced");
      socket = null;
      if (!stopped) connect();
    },
  };
}

// ============================================================
// Action State Reducer
// ============================================================

const initialDriverActionState: DriverActionState = {
  socketSynchronized: false,
  start: null,
  departure: null,
};

function reduceDriverRefreshEvent(
  state: DriverActionState,
  message: DriverRefreshMessage
): DriverActionState {
  const tripId = typeof message.data.trip_id === "string"
    ? message.data.trip_id
    : null;

  switch (message.event) {
    case "channel.connected":
      return { socketSynchronized: false, start: null, departure: null };

    case "trip.created":
      return { ...state, start: null, departure: null };

    case "trip.start_allowed": {
      if (!tripId) return state;
      return {
        ...state,
        start: {
          tripId,
          allowed: true,
          windowEndsAt:
            typeof message.data.start_window_ends_at === "string"
              ? message.data.start_window_ends_at
              : null,
        },
      };
    }

    case "trip.started": {
      if (!tripId) return state;
      return {
        ...state,
        start: state.start?.tripId === tripId ? null : state.start,
        departure: state.departure?.tripId === tripId ? null : state.departure,
      };
    }

    case "trip.stop_arrived": {
      if (!tripId) return state;
      const stopId = typeof message.data.stop_id === "string"
        ? message.data.stop_id
        : "";
      return {
        ...state,
        departure: {
          tripId,
          stopId,
          sequenceNo:
            typeof message.data.sequence_no === "number"
              ? message.data.sequence_no
              : null,
          allowed: false,
        },
      };
    }

    case "trip.departure_allowed": {
      const stopId = typeof message.data.stop_id === "string"
        ? message.data.stop_id
        : null;
      if (!tripId || !stopId) return state;
      return {
        ...state,
        departure: {
          tripId,
          stopId,
          sequenceNo:
            typeof message.data.sequence_no === "number"
              ? message.data.sequence_no
              : null,
          allowed: true,
        },
      };
    }

    case "trip.stop_departed": {
      const stopId = typeof message.data.stop_id === "string"
        ? message.data.stop_id
        : null;
      const isMatchingCandidate =
        state.departure?.tripId === tripId &&
        state.departure?.stopId === stopId;
      return isMatchingCandidate ? { ...state, departure: null } : state;
    }

    case "trip.completed":
    case "trip.cancelled":
    case "trip.premature_ended": {
      if (!tripId) return state;
      return {
        ...state,
        start: state.start?.tripId === tripId ? null : state.start,
        departure: state.departure?.tripId === tripId ? null : state.departure,
      };
    }

    default:
      return state;
  }
}

// ============================================================
// Helper Functions
// ============================================================

const getToken = async (): Promise<string | null> => {
  try {
    const { value } = await Preferences.get({ key: 'access_token' });
    return value || null;
  } catch (error) {
    console.error('Error getting token:', error);
    return null;
  }
};

interface StopWithTime {
  stop_id: string;
  name: string;
  sequence: number;
  assume_time_diff_minutes: number;
  boarding_allowed: boolean;
  deboarding_allowed: boolean;
  estimated_arrival?: string;
  cumulative_minutes?: number;
  travel_time_from_prev: number;
  arrival_time?: string | null;
  departure_time?: string | null;
  lat?: number;
  lng?: number;
}

interface NearStopInfo {
  isNear: boolean;
  stop: {
    id: string;
    name: string;
    lat: number;
    lng: number;
    radius_meters: number;
  } | null;
  distance_meters: number | null;
  message: string | null;
  hasNotified: boolean;
}

type ReactNode = import("react").ReactNode;
interface Passenger {
  fare_amount: ReactNode;
  booking_id: string;
  passenger_id: string;
  account_owner_user_id?: string;
  booked_by_user_id?: string;
  passenger_name: string;
  traveller_name?: string;
  traveller_phone?: string;
  traveller_email?: string;
  traveller_relationship_label?: string;
  account_owner_name?: string;
  seat_number?: number;
  otp?: string;
  status?: string;
  booking_status?: string;
  pickup_stop: {
    id: string;
    name: string;
  };
  dropoff_stop: {
    id: string;
    name: string;
  };
  fare: number;
  boarded_at?: string | null;
  completed_at?: string | null;
}

interface CurrentTripPassengersResponse {
  trip_id: string;
  total_passengers: number;
  passengers: Passenger[];
}

interface StopPassengerDetails {
  trip_id: string;
  stop_id: string;
  boarding_count: number;
  drop_count: number;
  boarding_passengers: Passenger[];
  drop_passengers: Passenger[];
}

const getDisplayName = (passenger: Passenger): string => {
  return passenger.traveller_name || passenger.passenger_name || "Unknown Passenger";
};

const getContactInfo = (passenger: Passenger): { phone: string; email: string } => {
  return {
    phone: passenger.traveller_phone || "",
    email: passenger.traveller_email || ""
  };
};

const getSeatDisplay = (seatNumber?: number | null): string => {
  if (seatNumber && seatNumber > 0) {
    return `Seat ${seatNumber}`;
  }
  return 'Seat —';
};

const SeatBadge: React.FC<{ seatNumber?: number | null }> = ({ seatNumber }) => {
  if (!seatNumber || seatNumber <= 0) return null;
  return (
    <span style={{
      display: 'inline-flex',
      alignItems: 'center',
      gap: '4px',
      padding: '2px 8px',
      background: 'linear-gradient(135deg, #8B5CF6 0%, #6366F1 100%)',
      borderRadius: '20px',
      fontSize: '10px',
      fontWeight: '600',
      color: '#FFFFFF',
      boxShadow: '0 2px 4px rgba(139, 92, 246, 0.3)'
    }}>
      <FiGrid size={10} />
      {getSeatDisplay(seatNumber)}
    </span>
  );
};

const PassengerCard: React.FC<{ passenger: Passenger; type: 'boarding' | 'dropping'; styles: any }> = ({ passenger, type, styles }) => {
  const displayName = getDisplayName(passenger);
  const contactInfo = getContactInfo(passenger);
  
  return (
    <div style={styles.passengerCard as React.CSSProperties}>
      <div style={styles.passengerHeader as React.CSSProperties}>
        <div style={styles.passengerAvatar as React.CSSProperties}>
          <FiUser size={16} />
        </div>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' as const }}>
            <p style={styles.passengerName as React.CSSProperties}>{displayName}</p>
            <SeatBadge seatNumber={passenger.seat_number} />
          </div>
          <p style={styles.passengerId as React.CSSProperties}>ID: {passenger.passenger_id?.slice(0, 8)}...</p>
        </div>
      </div>
      
      {(contactInfo.phone || contactInfo.email) && (
        <div style={styles.contactInfo as React.CSSProperties}>
          {contactInfo.phone && (
            <div style={styles.contactItem as React.CSSProperties}>
              <FiPhone size={12} />
              <span>{contactInfo.phone}</span>
            </div>
          )}
          {contactInfo.email && (
            <div style={styles.contactItem as React.CSSProperties}>
              <FiMail size={12} />
              <span>{contactInfo.email}</span>
            </div>
          )}
        </div>
      )}
      
      {passenger.traveller_relationship_label && (
        <div style={styles.relationshipLabel as React.CSSProperties}>
          {passenger.traveller_relationship_label}
        </div>
      )}
      
      <div style={styles.passengerDetails as React.CSSProperties}>
        <div style={styles.passengerStop as React.CSSProperties}>
          <FiMapPin size={12} style={{ color: '#10B981' }} />
          <span>Pickup: {passenger.pickup_stop?.name || 'N/A'}</span>
        </div>
        <div style={styles.passengerStop as React.CSSProperties}>
          <FiFlag size={12} style={{ color: '#EF4444' }} />
          <span>Drop: {passenger.dropoff_stop?.name || 'N/A'}</span>
        </div>
        <div style={styles.passengerFare as React.CSSProperties}>
          <FiDollarSign size={12} style={{ color: '#F59E0B' }} />
          <span>Fare: ₹{passenger.fare}</span>
        </div>
      </div>
      
      <div style={styles.passengerStatus as React.CSSProperties}>
        <span style={type === 'boarding' ? styles.statusBadgeBoarding as React.CSSProperties : styles.statusBadgeDropping as React.CSSProperties}>
          {passenger.booking_status || passenger.status || (type === 'boarding' ? 'Boarding' : 'Dropping')}
        </span>
      </div>
    </div>
  );
};

// ============================================================
// Main Component
// ============================================================

const CurrentTrip: React.FC = () => {
  
  // ============================================================
  // State
  // ============================================================
  
  const [token, setToken] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [trip, setTrip] = useState<any>(null);
  const [route, setRoute] = useState<any>(null);
  const [calculatedStops, setCalculatedStops] = useState<StopWithTime[]>([]);
  const [totalDuration, setTotalDuration] = useState({ totalMinutes: 0, hours: 0, minutes: 0 });
  const [showScanner, setShowScanner] = useState(false);
  const [scanResult, setScanResult] = useState<any>(null);
  const [showToast, setShowToast] = useState(false);
  const [toastMessage, setToastMessage] = useState("");
  const [toastColor, setToastColor] = useState("success");
  const [isDarkMode, setIsDarkMode] = useState(true);
  
  // WebSocket State
  const [wsState, setWsState] = useState<ConnectionState>("disconnected");
  const [actionState, setActionState] = useState<DriverActionState>(initialDriverActionState);
  const wsRef = useRef<ReturnType<typeof createDriverRefreshSocket> | null>(null);
  const tripRef = useRef<any>(null);
  
  // Permission States
  const [hasCameraPermission, setHasCameraPermission] = useState<boolean | null>(null);
  const [hasLocationPermission, setHasLocationPermission] = useState<boolean | null>(null);
  const [showPermissionModal, setShowPermissionModal] = useState(false);
  const [pendingAction, setPendingAction] = useState<'scan' | 'startTrip' | null>(null);
  
  // OTP Verification State
  const [showOtpModal, setShowOtpModal] = useState(false);
  const [otpCode, setOtpCode] = useState("");
  const [verifyingOtp, setVerifyingOtp] = useState(false);
  const [lastVerifiedSeat, setLastVerifiedSeat] = useState<number | null>(null);
  
  // Near Stop Detection State
  const [nearStopInfo, setNearStopInfo] = useState<NearStopInfo>({
    isNear: false,
    stop: null,
    distance_meters: null,
    message: null,
    hasNotified: false
  });
  const [checkingNearStop, setCheckingNearStop] = useState(false);
  const [lastCheckedLocation, setLastCheckedLocation] = useState<{ lat: number; lng: number } | null>(null);
  const locationIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastNotifiedStopIdRef = useRef<string | null>(null);
  
  const [showCancelModal, setShowCancelModal] = useState(false);
  const [cancelReason, setCancelReason] = useState("");
  const [cancelTripId, setCancelTripId] = useState<string | null>(null);
  const [showEmergencyModal, setShowEmergencyModal] = useState(false);
  const [emergencyReason, setEmergencyReason] = useState("");
  const [emergencyTripId, setEmergencyTripId] = useState<string | null>(null);
  
  const [cancelCharCount, setCancelCharCount] = useState(0);
  const [emergencyCharCount, setEmergencyCharCount] = useState(0);

  // Passenger Details State
  const [selectedStopForPassengers, setSelectedStopForPassengers] = useState<string | null>(null);
  const [passengerDetails, setPassengerDetails] = useState<StopPassengerDetails | null>(null);
  const [showPassengerModal, setShowPassengerModal] = useState(false);
  const [loadingPassengers, setLoadingPassengers] = useState(false);
  
  // Current Trip Passengers State
  const [currentTripPassengers, setCurrentTripPassengers] = useState<CurrentTripPassengersResponse | null>(null);
  const [showPassengersManifest, setShowPassengersManifest] = useState(false);
  const [loadingPassengersManifest, setLoadingPassengersManifest] = useState(false);

  // Refresh trigger for forced updates
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  
  // Start mutation loading state
  const [isStartingTrip, setIsStartingTrip] = useState(false);
  // Depart mutation loading state
  const [isDeparting, setIsDeparting] = useState(false);

  // ============================================================
  // WebSocket Setup
  // ============================================================
  
  useEffect(() => {
    const loadToken = async () => {
      setToken(await getToken());
    };
    void loadToken();
  }, []);

  useEffect(() => {
    tripRef.current = trip;
  }, [trip]);

  useEffect(() => {
    if (!token) return;

    initWebSocket(token);
    return () => {
      wsRef.current?.stop();
      wsRef.current = null;
    };
  }, [token]);

  const initWebSocket = (accessToken: string) => {
    if (wsRef.current) {
      wsRef.current.stop();
      wsRef.current = null;
    }
    
    const ws = createDriverRefreshSocket({
      apiBaseUrl: API_BASE,
      accessToken: accessToken,
      onEvent: (event) => {
        setActionState((prev) => reduceDriverRefreshEvent(prev, event));
      },
      onRefreshBatch: (events) => {
        const hasConnected = events.some(
          (event) => event.event === "channel.connected"
        );

        // channel.connected is emitted on every initial connection/reconnect.
        // Mark synchronization complete every time, not only on first mount.
        if (hasConnected) {
          setActionState((prev) => ({
            ...prev,
            socketSynchronized: true,
          }));
          void fetchTripDetails();
        }

        const hasTripEvent = events.some((event) =>
          event.event === "trip.created" ||
          event.event === "trip.started" ||
          event.event === "trip.stop_arrived" ||
          event.event === "trip.stop_departed" ||
          event.event === "trip.completed" ||
          event.event === "trip.cancelled" ||
          event.event === "trip.premature_ended" ||
          event.event === "booking.changed" ||
          event.event === "passenger.scan_completed"
        );

        if (hasTripEvent) {
          void fetchTripDetails();
          void fetchCurrentTripPassengers();
        }
      },
      onConnectionState: (state) => {
        setWsState(state);
        if (state !== "connected") {
          setActionState((prev) => ({ ...prev, socketSynchronized: false }));
        }
        if (state === "disconnected") {
          setActionState(initialDriverActionState);
        }
      },
      onAuthenticationFailure: () => {
        showToastNotification('Authentication failed. Please login again.', "danger");
        Preferences.remove({ key: 'access_token' });
        window.location.href = '/login';
      },
      onProtocolError: (value) => {
        console.error("WebSocket protocol error:", value);
      },
    });
    
    wsRef.current = ws;
    ws.start();
  };

  // ============================================================
  // Derived Action Eligibility
  // ============================================================
  
  const getCanStart = useCallback(() => {
    const currentTripId = trip?.trip_id || trip?.id;
    return (
      actionState.socketSynchronized &&
      actionState.start?.allowed === true &&
      actionState.start?.tripId === currentTripId &&
      trip?.status === "scheduled"
    );
  }, [actionState.socketSynchronized, actionState.start, trip]);

  const getCanDepart = useCallback((stopId: string) => {
    const currentTripId = trip?.trip_id || trip?.id;
    return (
      actionState.socketSynchronized &&
      actionState.departure?.allowed === true &&
      actionState.departure?.tripId === currentTripId &&
      actionState.departure?.stopId === stopId &&
      trip?.status === "in_progress"
    );
  }, [actionState.socketSynchronized, actionState.departure, trip]);

  // ============================================================
  // Effects
  // ============================================================
  
  useEffect(() => {
    const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    setIsDarkMode(prefersDark);
  }, []);

  useEffect(() => {
    if (token) {
      fetchTripDetails();
      checkLocationPermissionOnLoad();
    }
  }, [token]);

  useEffect(() => {
    if (refreshTrigger > 0 && token) {
      fetchTripDetails();
    }
  }, [refreshTrigger, token]);

  // Trip status effects
  useEffect(() => {
    if (trip?.status === "in_progress" && token) {
      startLocationTracking();
      void fetchCurrentTripPassengers();
    } else {
      stopLocationTracking();
      setCurrentTripPassengers(null);
    }

    return () => {
      stopLocationTracking();
    };
  }, [trip?.status, token, trip?.trip_id, trip?.id]);

  // ============================================================
  // API Functions
  // ============================================================
  
  const fetchCurrentTripPassengers = async () => {
    const latestTrip = tripRef.current;
    const tripId = latestTrip?.trip_id || latestTrip?.id;
    if (!tripId || !token) return;
    
    setLoadingPassengersManifest(true);
    try {
      const response = await fetch(
        `${API_BASE}/driver/trips/current/passengers`,
        {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
          }
        }
      );
      
      const data = await response.json();
      
      if (!response.ok) {
        console.error("Failed to fetch current trip passengers:", data);
        return;
      }
      
      setCurrentTripPassengers(data);
    } catch (err: any) {
      console.error("Error fetching current trip passengers:", err);
    } finally {
      setLoadingPassengersManifest(false);
    }
  };

  const checkCameraPermission = async (): Promise<boolean> => {
    try {
      if (navigator.permissions && navigator.permissions.query) {
        const result = await navigator.permissions.query({ name: 'camera' as PermissionName });
        if (result.state === 'granted') {
          setHasCameraPermission(true);
          return true;
        } else if (result.state === 'denied') {
          setHasCameraPermission(false);
          return false;
        }
      }
      
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      stream.getTracks().forEach(track => track.stop());
      setHasCameraPermission(true);
      return true;
    } catch (err: any) {
      console.error("Camera permission error:", err);
      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        setHasCameraPermission(false);
      }
      return false;
    }
  };

  const checkLocationPermission = async (): Promise<boolean> => {
    try {
      if (navigator.permissions && navigator.permissions.query) {
        const result = await navigator.permissions.query({ name: 'geolocation' });
        if (result.state === 'granted') {
          setHasLocationPermission(true);
          return true;
        } else if (result.state === 'denied') {
          setHasLocationPermission(false);
          return false;
        }
      }
      
      const position = await new Promise<GeolocationPosition>((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(resolve, reject, {
          enableHighAccuracy: true,
          timeout: 5000
        });
      });
      
      if (position) {
        setHasLocationPermission(true);
        return true;
      }
      return false;
    } catch (err: any) {
      console.error("Location permission error:", err);
      if (err.code === 1) {
        setHasLocationPermission(false);
      }
      return false;
    }
  };

  const checkLocationPermissionOnLoad = async () => {
    await checkLocationPermission();
  };

  const requestCameraPermission = async (): Promise<boolean> => {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ video: true });
      stream.getTracks().forEach(track => track.stop());
      setHasCameraPermission(true);
      showToastNotification('Camera access granted!', "success");
      return true;
    } catch (err: any) {
      console.error("Camera permission request error:", err);
      if (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError') {
        setHasCameraPermission(false);
        showToastNotification('Camera permission denied. Please enable camera access in your browser settings.', "danger");
      } else if (err.name === 'NotFoundError') {
        showToastNotification('No camera found on this device.', "danger");
      } else {
        showToastNotification('Failed to access camera. Please check your permissions.', "danger");
      }
      return false;
    }
  };

  const requestLocationPermission = async (): Promise<boolean> => {
    try {
      const position = await new Promise<GeolocationPosition>((resolve, reject) => {
        navigator.geolocation.getCurrentPosition(resolve, reject, {
          enableHighAccuracy: true,
          timeout: 10000,
          maximumAge: 0
        });
      });
      
      if (position) {
        setHasLocationPermission(true);
        showToastNotification('Location access granted!', "success");
        return true;
      }
      return false;
    } catch (err: any) {
      console.error("Location permission request error:", err);
      if (err.code === 1) {
        setHasLocationPermission(false);
        showToastNotification('Location permission denied. Please enable location access in your browser settings to start the trip.', "danger");
      } else {
        showToastNotification('Failed to get location. Please check your GPS settings.', "danger");
      }
      return false;
    }
  };

  const handleScanClick = async () => {
    if (!trip) {
      showToastNotification('No active trip found', "danger");
      return;
    }
    
    const hasPermission = await checkCameraPermission();
    
    if (!hasPermission) {
      setPendingAction('scan');
      setShowPermissionModal(true);
      return;
    }
    
    setShowScanner(true);
  };

  const handleStartTripWithPermission = async (tripId: string) => {
    if (!tripId || !token) {
      showToastNotification('No trip ID found', "danger");
      return;
    }
    
    if (!getCanStart()) {
      showToastNotification('Start is not currently allowed. Please wait for the scheduled time.', "warning");
      return;
    }
    
    const hasPermission = await checkLocationPermission();
    
    if (!hasPermission) {
      setPendingAction('startTrip');
      setShowPermissionModal(true);
      setCancelTripId(tripId);
      return;
    }
    
    await startTrip(tripId);
  };

  const startTrip = async (tripId: string) => {
    if (isStartingTrip) return;
    
    setIsStartingTrip(true);
    setLoading(true);
    try {
      const position = await getCurrentLocation();
      
      const formData = new FormData();
      formData.append("lat", position.lat.toString());
      formData.append("lng", position.lng.toString());
      
      const res = await fetch(`${API_BASE}/driver/scheduled-trips/${tripId}/start`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      
      const data = await res.json();
      
      if (!res.ok) {
        throw new Error(data.detail || data.error || data.message || "Failed to start trip");
      }
      
      showToastNotification('Trip started successfully!', "success");
      
      setActionState((prev) => ({
        ...prev,
        start: null,
      }));
      
      setTimeout(() => {
        fetchTripDetails();
      }, 1000);
      
    } catch (err: any) {
      console.error("Start trip error:", err);
      showToastNotification(err.message || 'Unknown error', "danger");
      fetchTripDetails();
    } finally {
      setLoading(false);
      setIsStartingTrip(false);
    }
  };

  const handleGrantPermission = async () => {
    setShowPermissionModal(false);
    
    if (pendingAction === 'scan') {
      const granted = await requestCameraPermission();
      if (granted) {
        setShowScanner(true);
      }
    } else if (pendingAction === 'startTrip' && cancelTripId) {
      const granted = await requestLocationPermission();
      if (granted && cancelTripId) {
        await startTrip(cancelTripId);
      }
    }
    
    setPendingAction(null);
  };

  const getCurrentLocation = (): Promise<{ lat: number; lng: number }> => {
    return new Promise((resolve, reject) => {
      if (!navigator.geolocation) {
        reject(new Error("Geolocation not supported"));
      } else {
        navigator.geolocation.getCurrentPosition(
          (position) => resolve({ lat: position.coords.latitude, lng: position.coords.longitude }),
          (err) => reject(err),
          { enableHighAccuracy: true, timeout: 10000, maximumAge: 0 }
        );
      }
    });
  };

  const showToastNotification = (message: string, color: string = "warning") => {
    setToastMessage(message);
    setToastColor(color);
    setShowToast(true);
    setTimeout(() => setShowToast(false), 4000);
  };

  // FIXED: View Passengers - fetches and shows passenger details in popup
  const fetchStopPassengerDetails = async (stopId: string) => {
    const tripId = trip?.trip_id || trip?.id;
    if (!tripId || !token) {
      showToastNotification('No active trip found', "danger");
      return;
    }

    setLoadingPassengers(true);
    try {
      const response = await fetch(
        `${API_BASE}/driver/trips/stop-passengers?trip_id=${tripId}&stop_id=${stopId}`,
        {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
          }
        }
      );

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || data.message || "Failed to fetch passenger details");
      }

      setPassengerDetails(data);
      setSelectedStopForPassengers(stopId);
      setShowPassengerModal(true);
      
    } catch (err: any) {
      console.error("Error fetching passenger details:", err);
      showToastNotification(err.message || 'Failed to fetch passenger details', "danger");
    } finally {
      setLoadingPassengers(false);
    }
  };

  const verifyOtp = async () => {
    const tripId = trip?.trip_id || trip?.id;
    if (!tripId || !token) {
      showToastNotification('No active trip found', "danger");
      return;
    }

    if (!otpCode || otpCode.length !== 6) {
      showToastNotification('Please enter a valid 6-digit OTP', "warning");
      return;
    }

    setVerifyingOtp(true);
    try {
      const position = await getCurrentLocation();
      
      const response = await fetch(`${API_BASE}/driver/otp/${tripId}/verify`, {
        method: "POST",
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        },
        body: JSON.stringify({
          otp_code: otpCode,
          lat: position.lat,
          lng: position.lng
        })
      });

      const data = await response.json();

      if (!response.ok) {
        throw new Error(data.detail || data.message || "OTP verification failed");
      }

      const seatNumber = data.seat_number;
      setLastVerifiedSeat(seatNumber || null);
      
      const successMessage = seatNumber 
        ? `✅ Passenger verified! Seat ${seatNumber} - ${data.scan_type === 'board' ? 'Boarded' : 'Dropped off'} successfully`
        : `✅ Passenger verified! ${data.scan_type === 'board' ? 'Boarded' : 'Dropped off'} successfully`;
      
      showToastNotification(successMessage, "success");
      setShowOtpModal(false);
      setOtpCode("");
      
      setTimeout(() => setLastVerifiedSeat(null), 3000);
      
      fetchTripDetails();
      fetchCurrentTripPassengers();
      
    } catch (err: any) {
      console.error("OTP verification error:", err);
      showToastNotification(err.message || 'OTP verification failed', "danger");
    } finally {
      setVerifyingOtp(false);
    }
  };

  const checkNearStop = async (lat: number, lng: number) => {
    const tripId = trip?.trip_id || trip?.id;
    if (!tripId || !token) return;
    
    setCheckingNearStop(true);
    try {
      const response = await fetch(
        `${API_BASE}/driver/trips/${tripId}/near-stop?lat=${lat}&lng=${lng}`,
        {
          headers: {
            'Authorization': `Bearer ${token}`,
            'Content-Type': 'application/json'
          }
        }
      );
      
      const data = await response.json();
      console.log("Near stop response:", data);
      
      if (response.ok && data.stop) {
        const currentStopId = data.stop.id;
        const distance = data.distance_meters || 0;
        const hasArrived = distance === 0;
        const isNewStop = lastNotifiedStopIdRef.current !== currentStopId;
        
        setNearStopInfo({
          isNear: true,
          stop: data.stop,
          distance_meters: distance,
          message: data.message || "You are near the stop",
          hasNotified: nearStopInfo.hasNotified
        });
        
        if (isNewStop && !hasArrived) {
          const distanceText = `${Math.round(distance)}m away`;
          showToastNotification(`📍 Approaching ${data.stop.name} - ${distanceText}`, "warning");
          lastNotifiedStopIdRef.current = currentStopId;
          setNearStopInfo(prev => ({ ...prev, hasNotified: true }));
        } 
        else if (hasArrived && lastNotifiedStopIdRef.current !== currentStopId) {
          showToastNotification(`✅ Arrived at ${data.stop.name}! Get ready to board/deboard passengers.`, "success");
          setNearStopInfo(prev => ({ ...prev, hasNotified: false }));
        }
      } else {
        if (nearStopInfo.isNear) {
          setNearStopInfo({
            isNear: false,
            stop: null,
            distance_meters: null,
            message: null,
            hasNotified: false
          });
        }
      }
    } catch (error) {
      console.error("Error checking near stop:", error);
    } finally {
      setCheckingNearStop(false);
    }
  };

  const startLocationTracking = () => {
    if (locationIntervalRef.current) {
      clearInterval(locationIntervalRef.current);
    }
    
    getCurrentLocation()
      .then(({ lat, lng }) => {
        setLastCheckedLocation({ lat, lng });
        checkNearStop(lat, lng);
      })
      .catch(err => console.error("Initial location error:", err));
    
    locationIntervalRef.current = setInterval(() => {
      getCurrentLocation()
        .then(({ lat, lng }) => {
          if (lastCheckedLocation) {
            const distance = Math.sqrt(
              Math.pow(lat - lastCheckedLocation.lat, 2) + 
              Math.pow(lng - lastCheckedLocation.lng, 2)
            ) * 111000;
            if (distance < 10) return;
          }
          setLastCheckedLocation({ lat, lng });
          checkNearStop(lat, lng);
        })
        .catch(err => console.error("Location tracking error:", err));
    }, 5000);
  };

  const stopLocationTracking = () => {
    if (locationIntervalRef.current) {
      clearInterval(locationIntervalRef.current);
      locationIntervalRef.current = null;
    }
    setNearStopInfo({ isNear: false, stop: null, distance_meters: null, message: null, hasNotified: false });
    lastNotifiedStopIdRef.current = null;
  };

  const formatTimeWithAmPm = (date: Date) => {
    return date.toLocaleTimeString('en-US', { 
      hour: 'numeric', 
      minute: '2-digit',
      hour12: true 
    });
  };

  const formatDateWithAmPm = (dateString: string) => {
    if (!dateString) return "-";
    const date = new Date(dateString);
    return formatTimeWithAmPm(date);
  };

  const calculateStopTimes = (stops: any[], tripStartTime: string): StopWithTime[] => {
    if (!stops || stops.length === 0) return [];
    
    let cumulativeMinutes = 0;
    const startDate = new Date(tripStartTime);
    
    return stops.map((stop, index) => {
      const timeDiff = stop.assume_time_diff_minutes || 0;
      cumulativeMinutes += timeDiff;
      const travelTimeFromPrev = index === 0 ? 0 : timeDiff;
      const arrivalDate = new Date(startDate.getTime() + cumulativeMinutes * 60 * 1000);
      const estimatedArrival = formatTimeWithAmPm(arrivalDate);
      
      return {
        stop_id: stop.stop_id,
        name: stop.name,
        sequence: stop.sequence,
        assume_time_diff_minutes: timeDiff,
        boarding_allowed: stop.boarding_allowed || false,
        deboarding_allowed: stop.deboarding_allowed || false,
        cumulative_minutes: cumulativeMinutes,
        travel_time_from_prev: travelTimeFromPrev,
        estimated_arrival: estimatedArrival,
        arrival_time: stop.arrival_time || null,
        departure_time: stop.departure_time || null,
        lat: stop.lat,
        lng: stop.lng
      };
    });
  };

  const calculateTotalDuration = (stops: StopWithTime[]) => {
    if (!stops || stops.length === 0) return { totalMinutes: 0, hours: 0, minutes: 0 };
    const totalMinutes = stops.reduce((total, stop) => total + (stop.assume_time_diff_minutes || 0), 0);
    const hours = Math.floor(totalMinutes / 60);
    const minutes = totalMinutes % 60;
    return { totalMinutes, hours, minutes };
  };

  const fetchTripDetails = async () => {
    if (!token) return;
    
    setLoading(true);
    try {
      const res = await fetch(`${API_BASE}/driver/trips/current`, {
        headers: { Authorization: `Bearer ${token}` },
      });
      
      if (res.status === 204) {
        clearTripStates();
        setLoading(false);
        return;
      }
      
      const data = await res.json();
      console.log("Current trip response:", data);
      
      if (data?.detail?.error === "no_active_trip" || data?.error === "no_active_trip") {
        clearTripStates();
        setLoading(false);
        return;
      }
      
      let tripData = data?.trip;
      if (!tripData && data?.trip_id) {
        tripData = data;
      }
      
      if (!tripData || !tripData.id) {
        clearTripStates();
        setLoading(false);
        return;
      }
      
      const tripId = tripData.id;
      console.log("Trip ID:", tripId, "Status:", tripData.status);
      
      if (tripData.status === "scheduled") {
        setTrip(tripData);
        setRoute(null);
        setCalculatedStops([]);
        setTotalDuration({ totalMinutes: 0, hours: 0, minutes: 0 });
        setLoading(false);
        return;
      }
      
      try {
        const detailsRes = await fetch(
          `${API_BASE}/driver/trips/${tripId}/details`,
          { headers: { Authorization: `Bearer ${token}` } }
        );
        
        if (detailsRes.ok) {
          const detailsData = await detailsRes.json();
          console.log("Trip details:", detailsData);
          
          if (detailsData) {
            setTrip(detailsData);
            setRoute(detailsData.route);
            
            if (detailsData.route?.stops?.length > 0) {
              const startTime = detailsData.planned_start_at || detailsData.planned_start;
              const calculated = calculateStopTimes(detailsData.route.stops, startTime);
              setCalculatedStops(calculated);
              setTotalDuration(calculateTotalDuration(calculated));
            }
          }
        } else {
          setTrip(tripData);
        }
      } catch (err) {
        console.error("Error fetching details:", err);
        setTrip(tripData);
      }
      
    } catch (err: any) {
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const handleScanSuccess = (data: any) => {
    setScanResult(data);
    if (data.error) {
      showToastNotification(data.error, "danger");
    } else {
      const seatMsg = data.seat_number ? ` (Seat ${data.seat_number})` : '';
      showToastNotification(`Passenger verified successfully${seatMsg}!`, "success");
    }
    setTimeout(() => setScanResult(null), 5000);
    fetchTripDetails();
    fetchCurrentTripPassengers();
  };

  // Stop actions use backend WebSocket eligibility as the action authority.
  const handleStopAction = async (stop_id: string, mode: "arrive" | "depart") => {
    if (!trip || !token) return;
    
    // For depart, check WebSocket eligibility
    if (mode === "depart") {
      if (!getCanDepart(stop_id)) {
        showToastNotification('Departure is not currently allowed.', "warning");
        return;
      }
      setIsDeparting(true);
    }
    
    try {
      const position = await getCurrentLocation();
      
      const formData = new FormData();
      formData.append("stop_id", stop_id);
      formData.append("mode", mode);
      formData.append("driver_lat", position.lat.toString());
      formData.append("driver_lng", position.lng.toString());
      
      console.log(`${mode} at stop:`, { stop_id, mode, lat: position.lat, lng: position.lng });
      
      setLoading(true);
      const res = await fetch(`${API_BASE}/driver/scheduled-trips/${trip.trip_id || trip.id}/stop-action`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      
      const data = await res.json();
      
      if (!res.ok) {
        throw new Error(data.detail || data.error || "Failed to update stop");
      }
      
      showToastNotification(`${mode === "arrive" ? "Arrived at" : "Departed from"} stop successfully!`, "success");
      
      if (mode === "arrive") {
        lastNotifiedStopIdRef.current = null;
        setNearStopInfo((prev) => ({ ...prev, hasNotified: false }));
        // Do not write departure eligibility here. The backend emits
        // trip.stop_arrived followed by trip.departure_allowed when eligible.
      }

      if (mode === "depart") {
        setActionState((prev) => ({
          ...prev,
          departure: null
        }));
      }
      
      // Refresh trip details after action
      fetchTripDetails();
      
    } catch (err: any) {
      console.error("Stop action error:", err);
      showToastNotification(err.message || 'Failed to update stop', "danger");
      fetchTripDetails();
    } finally {
      setLoading(false);
      setIsDeparting(false);
    }
  };

  // FIXED: handleEndTrip - Normal end trip
  const handleEndTrip = async (tripId: string) => {
    if (!tripId || !token) return;
    setLoading(true);
    try {
      const position = await getCurrentLocation();
      const formData = new FormData();
      formData.append("actual_end_at", new Date().toISOString());
      formData.append("lat", position.lat.toString());
      formData.append("lng", position.lng.toString());
      
      const res = await fetch(`${API_BASE}/driver/scheduled-trips/${tripId}/end`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      
      const data = await res.json();
      
      if (!res.ok) throw new Error(data.detail || data.error || "Failed to end trip");
      
      showToastNotification('Trip ended successfully!', "success");
      
      clearTripStates();
      
      setTimeout(() => {
        fetchTripDetails();
      }, 500);
      
    } catch (err: any) {
      console.error("End trip error:", err);
      showToastNotification(err.message || 'Unknown error', "danger");
    } finally {
      setLoading(false);
    }
  };

  // FIXED: submitEmergencyStop - Emergency end with API call
  const submitEmergencyStop = async () => {
    if (!emergencyTripId || !emergencyReason || !token) {
      showToastNotification('Please provide a reason for emergency stop!', "danger");
      return;
    }
    
    if (emergencyReason.length < 5) {
      showToastNotification('Reason must be at least 5 characters long!', "danger");
      return;
    }
    
    setLoading(true);
    try {
      const position = await getCurrentLocation();
      const formData = new FormData();
      formData.append("reason", emergencyReason);
      formData.append("lat", position.lat.toString());
      formData.append("lng", position.lng.toString());
      formData.append("actual_end_at", new Date().toISOString());
      
      console.log("Emergency stop API call:", {
        tripId: emergencyTripId,
        reason: emergencyReason,
        lat: position.lat,
        lng: position.lng
      });
      
      const res = await fetch(`${API_BASE}/driver/scheduled-trips/${emergencyTripId}/emergency-end`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: formData,
      });
      
      const data = await res.json();
      console.log("Emergency stop response:", data);
      
      if (!res.ok) {
        let errorMsg = data.detail?.message || data.detail?.error || data.error || "Emergency stop failed";
        showToastNotification(errorMsg, "danger");
        return;
      }
      
      showToastNotification('Emergency stop completed successfully!', "success");
      setShowEmergencyModal(false);
      
      // Clear all trip states
      clearTripStates();
      
      // Refresh trip details
      setTimeout(() => {
        fetchTripDetails();
      }, 500);
      
    } catch (err: any) {
      console.error("Emergency stop error:", err);
      showToastNotification(err.message || 'Failed to complete emergency stop', "danger");
    } finally {
      setLoading(false);
    }
  };

  const handleCancelTrip = (tripId: string) => {
    setCancelTripId(tripId);
    setCancelReason("");
    setCancelCharCount(0);
    setShowCancelModal(true);
  };

  const submitCancelTrip = async () => {
    if (!cancelTripId || !cancelReason || !token) {
      showToastNotification('Please provide a reason for cancellation', "danger");
      return;
    }
    
    if (cancelReason.length < 100) {
      showToastNotification('Reason must be at least 100 characters long!', "danger");
      return;
    }
    
    setLoading(true);
    try {
      const fd = new FormData();
      fd.append("reason", cancelReason);
      
      const res = await fetch(`${API_BASE}/driver/trips/${cancelTripId}/cancel`, {
        method: "POST",
        headers: { Authorization: `Bearer ${token}` },
        body: fd,
      });
      
      const data = await res.json();
      
      if (!res.ok) {
        let errorMsg = data.detail?.message || data.detail?.error || data.error || "Cancel trip failed";
        showToastNotification(errorMsg, "danger");
        return;
      }
      
      showToastNotification('Trip cancelled successfully!', "success");
      setShowCancelModal(false);
      
      clearTripStates();
      setRefreshTrigger(prev => prev + 1);
      
    } catch (err: any) {
      console.error("Cancel trip error:", err);
      showToastNotification(err.message, "danger");
    } finally {
      setLoading(false);
    }
  };

  const clearTripStates = () => {
    setTrip(null);
    setRoute(null);
    setCalculatedStops([]);
    setCurrentTripPassengers(null);
    setNearStopInfo({
      isNear: false,
      stop: null,
      distance_meters: null,
      message: null,
      hasNotified: false
    });
    setTotalDuration({ totalMinutes: 0, hours: 0, minutes: 0 });
    
    stopLocationTracking();
    lastNotifiedStopIdRef.current = null;
    
    setActionState((prev) => ({
      ...prev,
      start: null,
      departure: null,
    }));
  };

  const getDistanceText = (meters: number): string => {
    if (meters === 0) return "📍 You have arrived!";
    if (meters < 50) return `🔴 Very close - ${Math.round(meters)}m away`;
    if (meters < 100) return `🟠 Getting close - ${Math.round(meters)}m away`;
    if (meters < 200) return `🟡 Approaching - ${Math.round(meters)}m away`;
    return `⚪ ${Math.round(meters)}m away`;
  };

  const openEmergencyStopModal = (tripId: string) => {
    setEmergencyTripId(tripId);
    setEmergencyReason("");
    setEmergencyCharCount(0);
    setShowEmergencyModal(true);
  };

  const styles = getStyles(isDarkMode, trip, nearStopInfo);

  // ============================================================
  // Render
  // ============================================================
  
  return (
    <IonPage>
      <NavbarSidebar />
      <IonContent style={{ '--background': isDarkMode ? '#000000' : '#F8F9FA' } as any}>
        <div style={styles.container}>
          
          {/* WebSocket Status Indicator */}
          <div style={styles.wsStatusBar}>
            <div style={{
              display: 'flex',
              alignItems: 'center',
              gap: '8px',
              padding: '6px 12px',
              borderRadius: '20px',
              background: wsState === 'connected' 
                ? (isDarkMode ? '#064E3B' : '#D1FAE5')
                : (isDarkMode ? '#7F1D1D' : '#FEE2E2'),
              fontSize: '12px',
              fontWeight: '500',
              color: wsState === 'connected'
                ? (isDarkMode ? '#A7F3D0' : '#065F46')
                : (isDarkMode ? '#FCA5A5' : '#991B1B'),
            }}>
              {wsState === 'connected' ? (
                <FiWifi size={14} />
              ) : (
                <FiWifiOff size={14} />
              )}
              {wsState === 'connected' ? 'Live' : wsState === 'connecting' ? 'Connecting...' : 'Offline'}
            </div>
            {!actionState.socketSynchronized && wsState === 'connected' && (
              <div style={{
                fontSize: '11px',
                color: isDarkMode ? '#F59E0B' : '#92400E',
                padding: '4px 8px',
                background: isDarkMode ? '#78350F40' : '#FEF3C7',
                borderRadius: '12px',
              }}>
                Syncing...
              </div>
            )}
          </div>
          
          <IonToast
            isOpen={showToast}
            onDidDismiss={() => setShowToast(false)}
            message={toastMessage}
            duration={4000}
            color={toastColor}
            position="top"
          />
          
          {loading && <IonLoading isOpen={loading} message="Processing..." />}
          
          {!trip && !loading && (
            <div style={styles.emptyState}>
              <FiTruck style={styles.emptyIcon} />
              <h2 style={styles.emptyTitle}>No Active Trip</h2>
              <p style={styles.emptyText}>
                You currently have no active or scheduled trips.
              </p>
            </div>
          )}
          
          {trip && (
            <>
              {/* Last Verified Seat Toast */}
              {lastVerifiedSeat && (
                <div style={{
                  background: isDarkMode ? '#064E3B' : '#D1FAE5',
                  border: `1px solid ${isDarkMode ? '#10B981' : '#059669'}`,
                  borderRadius: '12px',
                  padding: '12px 16px',
                  marginBottom: '16px',
                  display: 'flex',
                  alignItems: 'center',
                  gap: '12px'
                }}>
                  <FiUserCheck size={20} color="#10B981" />
                  <span style={{ fontWeight: '600', color: isDarkMode ? '#FFFFFF' : '#064E3B' }}>
                    Last verified: Seat {lastVerifiedSeat}
                  </span>
                </div>
              )}
              
              {/* Ready to Start Card */}
              {trip.status === "scheduled" && getCanStart() && (
                <div style={styles.readyToStartCard}>
                  <div style={styles.readyToStartHeader}>
                    <div style={{
                      width: '56px',
                      height: '56px',
                      borderRadius: '28px',
                      background: isDarkMode ? 'rgba(16, 185, 129, 0.2)' : 'rgba(16, 185, 129, 0.15)',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                    }}>
                      <FiPlay size={28} color="#10B981" />
                    </div>
                    <div>
                      <h3 style={styles.readyToStartTitle}>Ready to Start!</h3>
                      <p style={styles.readyToStartText}>
                        {actionState.start?.windowEndsAt 
                          ? `Start window ends at ${new Date(actionState.start.windowEndsAt).toLocaleTimeString()}`
                          : 'The start window is now open.'}
                      </p>
                    </div>
                  </div>
                  <button 
                    onClick={() => {
                      handleStartTripWithPermission(trip.trip_id || trip.id);
                    }} 
                    style={styles.readyToStartButton}
                    disabled={loading || isStartingTrip}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.transform = 'scale(1.02)';
                      e.currentTarget.style.background = '#059669';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.transform = 'scale(1)';
                      e.currentTarget.style.background = '#10B981';
                    }}
                  >
                    {isStartingTrip ? (
                      <>
                        <div className="animate-spin rounded-full h-5 w-5 border-b-2 border-white"></div>
                        Starting...
                      </>
                    ) : (
                      <>
                        <FiPlay size={18} />
                        Start Trip Now
                      </>
                    )}
                  </button>
                </div>
              )}
              
              {/* Scheduled but not yet eligible */}
              {trip.status === "scheduled" && !getCanStart() && (
                <div style={styles.waitingCard}>
                  <div style={styles.waitingContent}>
                    <FiClock size={24} color="#F59E0B" />
                    <div>
                      <h3 style={styles.waitingTitle}>Waiting for Start Window</h3>
                      <p style={styles.waitingText}>
                        The trip will be available to start at {formatDateWithAmPm(trip.planned_start_at || trip.planned_start)}.
                        {!actionState.socketSynchronized && ' Waiting for sync...'}
                      </p>
                    </div>
                  </div>
                </div>
              )}
              
              {/* Near Stop Detection Card */}
              {trip.status === "in_progress" && nearStopInfo.isNear && nearStopInfo.stop && (
                <div style={nearStopInfo.distance_meters === 0 ? styles.arrivedStopCard : styles.nearStopCard}>
                  <div style={styles.nearStopHeader}>
                    <div style={nearStopInfo.distance_meters === 0 ? styles.arrivedStopIcon : styles.nearStopIcon}>
                      {nearStopInfo.distance_meters === 0 ? (
                        <FiCheckCircle size={28} color="#10B981" />
                      ) : (
                        <FiTarget size={24} color="#F59E0B" />
                      )}
                    </div>
                    <div style={styles.nearStopContent}>
                      <div style={nearStopInfo.distance_meters === 0 ? styles.arrivedStopTitle : styles.nearStopTitle}>
                        {nearStopInfo.distance_meters === 0 ? (
                          <><FiCheckCircle size={12} /> Arrived at Stop</>
                        ) : (
                          <><FiBell size={14} /> Near By Stop</>
                        )}
                      </div>
                      <h3 style={nearStopInfo.distance_meters === 0 ? styles.arrivedStopName : styles.nearStopName}>
                        {nearStopInfo.stop.name}
                      </h3>
                      <p style={nearStopInfo.distance_meters === 0 ? styles.arrivedStopDistance : styles.nearStopDistance}>
                        {nearStopInfo.distance_meters !== null && getDistanceText(nearStopInfo.distance_meters)}
                      </p>
                      {nearStopInfo.distance_meters === 0 && (
                        <div style={styles.arrivalAlert}>
                          <FiCheckCircle size={16} />
                          <span>Ready to board/deboard passengers</span>
                        </div>
                      )}
                    </div>
                  </div>
                  <div style={styles.nearStopFooter}>
                    <div style={styles.radiusInfo}>
                      <FiCompass size={12} />
                      <span>Detection radius: {nearStopInfo.stop.radius_meters}m</span>
                    </div>
                    {checkingNearStop && (
                      <div style={styles.checkingBadge}>
                        <div className="animate-spin rounded-full h-3 w-3 border-b-2 border-current"></div>
                        <span>Updating...</span>
                      </div>
                    )}
                  </div>
                </div>
              )}
              
              {/* Trip Header Card */}
              <div style={styles.tripCard}>
                <div style={styles.tripHeader}>
                  <div>
                    <div style={styles.routeBadge}>
                      <FiMapPin style={styles.routeIcon} />
                      <span style={styles.routeName}>
                        {route?.name || trip.route?.name || trip.route_name || "Unnamed Route"}
                      </span>
                    </div>
                    <div style={styles.statusBadge}>
                      {trip.status === "scheduled" && <><FiClock style={styles.statusIcon} /> Scheduled</>}
                      {trip.status === "in_progress" && <><FiNavigation style={styles.statusIcon} /> In Progress</>}
                    </div>
                  </div>
                  <div style={{ display: 'flex', gap: '8px' }}>
                    <button onClick={() => setShowOtpModal(true)} style={styles.otpButton}>
                      <FiKey style={styles.scanIcon} />
                      Enter OTP
                    </button>
                    <button 
                      onClick={handleScanClick} 
                      style={styles.scanButton}
                    >
                      <FiCamera style={styles.scanIcon} />
                      Scan QR
                    </button>
                  </div>
                </div>
                
                <div style={styles.tripInfo}>
                  <div style={styles.infoItem}>
                    <FiCalendar style={styles.infoIcon} />
                    <div>
                      <p style={styles.infoLabel}>Trip ID</p>
                      <p style={styles.infoValue}>
                        {(trip.trip_id || trip.id)?.slice(0, 8)}...
                      </p>
                    </div>
                  </div>
                  <div style={styles.infoItem}>
                    <FiClock style={styles.infoIcon} />
                    <div>
                      <p style={styles.infoLabel}>Planned Start</p>
                      <p style={styles.infoValue}>
                        {formatDateWithAmPm(trip.planned_start_at || trip.planned_start)}
                      </p>
                    </div>
                  </div>
                  <div style={styles.infoItem}>
                    <FiFlag style={styles.infoIcon} />
                    <div>
                      <p style={styles.infoLabel}>Planned End</p>
                      <p style={styles.infoValue}>
                        {formatDateWithAmPm(trip.planned_end_at || trip.planned_end)}
                      </p>
                    </div>
                  </div>
                </div>
                
                {(trip.vehicle || trip.vehicle_id) && (
                  <div style={styles.vehicleCard}>
                    <div style={styles.vehicleInner}>
                      <FiTruck style={{ color: '#10B981', fontSize: '20px' }} />
                      <div>
                        <p style={styles.vehicleLabel}>Vehicle</p>
                        <p style={styles.vehicleValue}>
                          {trip.vehicle?.name || trip.vehicle_name || "Vehicle Assigned"}
                          {trip.vehicle?.registration_number && ` (${trip.vehicle.registration_number})`}
                        </p>
                      </div>
                    </div>
                  </div>
                )}
                
                {trip.status === "scheduled" && !getCanStart() && (
                  <div style={styles.scheduledMessage}>
                    <FiClock style={{ color: '#F59E0B', fontSize: '20px' }} />
                    <div>
                      <p style={styles.scheduledTitle}>Trip Scheduled</p>
                      <p style={styles.scheduledText}>
                        This trip is scheduled to start at {formatDateWithAmPm(trip.planned_start_at || trip.planned_start)}.
                        Please wait for the start time to begin the journey.
                        {!actionState.socketSynchronized && ' (Syncing...)'}
                      </p>
                    </div>
                  </div>
                )}
                
                {totalDuration.totalMinutes > 0 && trip.status !== "scheduled" && (
                  <div style={styles.totalDurationCard}>
                    <div style={styles.totalDurationInner}>
                      <FiClock style={{ color: '#10B981', fontSize: '20px' }} />
                      <div>
                        <p style={styles.totalDurationLabel}>Total Trip Duration</p>
                        <p style={styles.totalDurationValue}>{totalDuration.hours}h {totalDuration.minutes}m</p>
                      </div>
                    </div>
                  </div>
                )}
                
                {(trip.actual_start_at || trip.actual_start || trip.actual_end_at || trip.actual_end) && (
                  <div style={styles.actualTimesSection}>
                    <div style={styles.actualTimesHeader}>
                      <FiNavigation style={styles.actualIcon} />
                      <span style={styles.actualTitle}>Actual Times</span>
                    </div>
                    <div style={styles.actualTimesGrid}>
                      <div style={styles.actualTimeItem}>
                        <p style={styles.actualLabel}>Actual Start</p>
                        <p style={styles.actualValue}>
                          {(trip.actual_start_at || trip.actual_start) ? new Date(trip.actual_start_at || trip.actual_start).toLocaleString([], { 
                            day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: true 
                          }) : "-"}
                        </p>
                      </div>
                      <div style={styles.actualTimeItem}>
                        <p style={styles.actualLabel}>Actual End</p>
                        <p style={styles.actualValue}>
                          {(trip.actual_end_at || trip.actual_end) ? new Date(trip.actual_end_at || trip.actual_end).toLocaleString([], { 
                            day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit', hour12: true 
                          }) : trip.status === "in_progress" ? "In Progress" : "-"}
                        </p>
                      </div>
                    </div>
                  </div>
                )}
                
                <div style={styles.actionButtons}>
                  {trip.status === "scheduled" && (
                    <>
                      <button 
                        onClick={() => {
                          handleStartTripWithPermission(trip.trip_id || trip.id);
                        }} 
                        style={{
                          ...styles.startButton,
                          opacity: (!getCanStart() || isStartingTrip) ? 0.5 : 1,
                          cursor: (!getCanStart() || isStartingTrip) ? 'not-allowed' : 'pointer',
                          background: (!getCanStart() || isStartingTrip) ? '#6B7280' : '#10B981',
                        }}
                        disabled={!getCanStart() || isStartingTrip}
                      >
                        <FiPlay style={styles.buttonIcon} />
                        {isStartingTrip ? "Starting..." : "Start Trip"}
                      </button>
                      <button onClick={() => handleCancelTrip(trip.trip_id || trip.id)} style={styles.cancelButton} disabled={loading}>
                        <FiX style={styles.buttonIcon} />
                        Cancel Trip
                      </button>
                    </>
                  )}
                  {trip.status === "in_progress" && (
                    <>
                      <button 
                        onClick={() => handleEndTrip(trip.trip_id || trip.id)} 
                        style={styles.endButton} 
                        disabled={loading}
                      >
                        <FiSquare style={styles.buttonIcon} />
                        {loading ? "Ending..." : "End Trip"}
                      </button>
                      <button onClick={() => openEmergencyStopModal(trip.trip_id || trip.id)} style={styles.emergencyButton} disabled={loading}>
                        <FiAlertCircle style={styles.buttonIcon} />
                        Emergency
                      </button>
                    </>
                  )}
                </div>
              </div>
              
              {/* Current Trip Passengers Manifest Button */}
              {trip.status === "in_progress" && (
                <div style={styles.passengersManifestButtonWrapper}>
                  <button 
                    onClick={() => setShowPassengersManifest(true)} 
                    style={styles.viewManifestButton}
                  >
                    <FiUsers size={18} />
                    View All Passengers ({currentTripPassengers?.total_passengers || 0})
                  </button>
                </div>
              )}
              
              {trip.status === "in_progress" && calculatedStops.length > 0 && (
                <div style={styles.stopsSection}>
                  <div style={styles.stopsHeader}>
                    <div style={styles.stopsHeaderLeft}>
                      <FiMapPin style={{ color: '#10B981', fontSize: '20px' }} />
                      <h3 style={styles.stopsTitle}>Route Stops</h3>
                    </div>
                    <span style={styles.stopCount}>{calculatedStops.length} stops</span>
                  </div>

                  <div style={styles.stopsList}>
                    {calculatedStops.map((stop, index) => {
                      const isArrived = stop.arrival_time;
                      const isDeparted = stop.departure_time;
                      const isFirstStop = index === 0;
                      const isLastStop = index === calculatedStops.length - 1;
                      const isCurrent = !isArrived && isFirstStop;
                      const isNearThisStop = nearStopInfo.isNear && nearStopInfo.stop?.name === stop.name;
                      const hasArrivedAtStop = nearStopInfo.isNear && nearStopInfo.stop?.name === stop.name && nearStopInfo.distance_meters === 0;
                      
                      const canDepartThisStop = getCanDepart(stop.stop_id);
                      
                      return (
                        <div 
                          key={stop.stop_id} 
                          style={{
                            ...styles.stopCard,
                            ...(isNearThisStop ? styles.stopCardNear : {}),
                            ...(hasArrivedAtStop ? styles.stopCardArrived : {}),
                            ...(canDepartThisStop ? styles.stopCardDepartReady : {}),
                          }}
                        >
                          <div style={styles.stopCardInner}>
                            <div style={styles.stopNumberBadge}>
                              <span style={{
                                ...styles.stopNumber,
                                background: isDeparted ? '#10B981' : isArrived ? '#3B82F6' : isCurrent ? '#F59E0B' : (isDarkMode ? '#1F1F1F' : '#E5E7EB'),
                                color: (isDeparted || isArrived || isCurrent) ? '#FFFFFF' : (isDarkMode ? '#FFFFFF' : '#111827')
                              }}>{stop.sequence}</span>
                            </div>

                            <div style={styles.stopDetails}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '10px', flexWrap: 'wrap' as const }}>
                                <p style={styles.stopName}>{stop.name}</p>
                                {isNearThisStop && !hasArrivedAtStop && (
                                  <span style={styles.approachingBadge}>
                                    <FiTarget size={10} />
                                    APPROACHING
                                  </span>
                                )}
                                {hasArrivedAtStop && (
                                  <span style={styles.arrivedBadgeNew}>
                                    <FiCheckCircle size={10} />
                                    ARRIVED
                                  </span>
                                )}
                                {canDepartThisStop && (
                                  <span style={styles.departReadyBadge}>
                                    <FiArrowRightCircle size={10} />
                                    DEPART READY
                                  </span>
                                )}
                              </div>
                              
                              <div style={styles.timeGrid}>
                                {index > 0 && stop.travel_time_from_prev > 0 && (
                                  <div style={styles.timeBadge}>
                                    <div style={{ ...styles.timeDot, backgroundColor: '#F59E0B' }} />
                                    <span style={styles.timeLabel}>Travel:</span>
                                    <span style={styles.timeValue}>+{stop.travel_time_from_prev} min</span>
                                  </div>
                                )}
                                
                                {stop.cumulative_minutes && stop.cumulative_minutes > 0 && (
                                  <div style={styles.timeBadge}>
                                    <div style={{ ...styles.timeDot, backgroundColor: '#3B82F6' }} />
                                    <span style={styles.timeLabel}>From Start:</span>
                                    <span style={styles.timeValue}>
                                      {Math.floor(stop.cumulative_minutes / 60)}h {stop.cumulative_minutes % 60}m
                                    </span>
                                  </div>
                                )}
                                
                                {stop.estimated_arrival && (
                                  <div style={styles.timeBadge}>
                                    <div style={{ ...styles.timeDot, backgroundColor: '#10B981' }} />
                                    <span style={styles.timeLabel}>Est. Arrival:</span>
                                    <span style={styles.timeValue}>{stop.estimated_arrival}</span>
                                  </div>
                                )}
                              </div>

                              <div style={styles.actualStopTimes}>
                                {stop.arrival_time && (
                                  <div style={styles.actualTimeBadge}>
                                    <FiStopCircle style={{ fontSize: '10px', color: '#3B82F6' }} />
                                    <span style={styles.actualTimeLabel}>Arrived:</span>
                                    <span style={styles.actualTimeValue}>{formatDateWithAmPm(stop.arrival_time)}</span>
                                  </div>
                                )}
                                {stop.departure_time && (
                                  <div style={styles.actualTimeBadge}>
                                    <FiCheckCircle style={{ fontSize: '10px', color: '#10B981' }} />
                                    <span style={styles.actualTimeLabel}>Departed:</span>
                                    <span style={styles.actualTimeValue}>{formatDateWithAmPm(stop.departure_time)}</span>
                                  </div>
                                )}
                              </div>

                              <div style={styles.statusBadges}>
                                {stop.boarding_allowed && <span style={styles.boardingBadge}>✓ Boarding</span>}
                                {stop.deboarding_allowed && <span style={styles.deboardingBadge}>✓ Deboarding</span>}
                                {isArrived && !isDeparted && <span style={styles.arrivedBadge}>📍 Arrived</span>}
                                {isDeparted && <span style={styles.completedBadge}>✓ Completed</span>}
                                {canDepartThisStop && (
                                  <span style={styles.departAllowedBadge}>
                                    <FiArrowRightCircle size={10} />
                                    Depart Allowed
                                  </span>
                                )}
                              </div>
                              
                              {/* View Passengers Button */}
                              <div style={styles.stopActionButtons}>
                                <button 
                                  onClick={() => fetchStopPassengerDetails(stop.stop_id)} 
                                  style={styles.viewPassengersButton}
                                  disabled={loadingPassengers}
                                >
                                  <FiUsers size={14} />
                                  View Passengers
                                </button>
                                
                                {!isDeparted && (
                                  <>
                                    {!isFirstStop && !isArrived && (
                                      <button 
                                        onClick={() => handleStopAction(stop.stop_id, "arrive")} 
                                        style={styles.arriveStopButton} 
                                        disabled={loading}
                                      >
                                        <FiCheckCircle />
                                        Mark Arrival
                                      </button>
                                    )}
                                    
                                    {/* Departure is enabled only by the matching WS event. */}
                                    {!isLastStop && isArrived && !isDeparted && (
                                      <button 
                                        onClick={() => handleStopAction(stop.stop_id, "depart")} 
                                        style={{
                                          ...styles.departStopButton,
                                          opacity: (!canDepartThisStop || isDeparting) ? 0.5 : 1,
                                          cursor: (!canDepartThisStop || isDeparting) ? 'not-allowed' : 'pointer',
                                          background: (!canDepartThisStop || isDeparting) ? '#6B7280' : '#3B82F6',
                                        }}
                                        disabled={!canDepartThisStop || isDeparting}
                                      >
                                        <FiArrowRightCircle />
                                        {isDeparting ? 'Departing...' : 'Mark Departure'}
                                      </button>
                                    )}
                                    
                                    {isFirstStop && !isArrived && !isDeparted && (
                                      <button onClick={() => handleStopAction(stop.stop_id, "arrive")} style={styles.startJourneyButton} disabled={loading}>
                                        <FiPlay />
                                        Start Journey
                                      </button>
                                    )}
                                    
                                    {/* Complete Trip button */}
                                    {isLastStop && isArrived && !isDeparted && (
                                      <button 
                                        onClick={() => handleStopAction(stop.stop_id, "depart")} 
                                        style={{
                                          ...styles.completeTripButton,
                                          opacity: (!canDepartThisStop || isDeparting) ? 0.5 : 1,
                                          cursor: (!canDepartThisStop || isDeparting) ? 'not-allowed' : 'pointer',
                                          background: (!canDepartThisStop || isDeparting) ? '#6B7280' : '#8B5CF6',
                                        }}
                                        disabled={!canDepartThisStop || isDeparting}
                                      >
                                        <FiFlag />
                                        {isDeparting ? 'Completing...' : 'Complete Trip'}
                                      </button>
                                    )}
                                  </>
                                )}
                              </div>
                            </div>

                            {stop.cumulative_minutes && stop.cumulative_minutes > 0 && (
                              <div style={styles.cumulativeBadge}>
                                <p style={styles.cumulativeLabel}>Cumulative</p>
                                <p style={styles.cumulativeValue}>
                                  {Math.floor(stop.cumulative_minutes / 60)}h {stop.cumulative_minutes % 60}m
                                </p>
                              </div>
                            )}
                          </div>
                        </div>
                      );
                    })}
                  </div>

                  <div style={styles.journeySummary}>
                    <div style={styles.summaryInner}>
                      <FiNavigation style={{ width: '24px', height: '24px', color: '#10B981' }} />
                      <div style={styles.summaryContent}>
                        <p style={styles.summaryTitle}>Journey Summary</p>
                        <p style={styles.summaryText}>
                          {calculatedStops.length} stops • Total travel time: {totalDuration.hours}h {totalDuration.minutes}m
                        </p>
                      </div>
                      <div style={styles.summaryTimes}>
                        <div style={styles.summaryTimeItem}>
                          <p style={styles.summaryTimeLabel}>Start Time</p>
                          <p style={styles.summaryTimeValue}>{formatDateWithAmPm(trip.planned_start_at || trip.planned_start)}</p>
                        </div>
                        <div style={styles.summaryTimeItem}>
                          <p style={styles.summaryTimeLabel}>End Time</p>
                          <p style={styles.summaryTimeValue}>{formatDateWithAmPm(trip.planned_end_at || trip.planned_end)}</p>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              )}
            </>
          )}
          
          {/* ============================================================ */}
          {/* ALL MODALS */}
          {/* ============================================================ */}
          
          {/* Permission Request Modal */}
          {showPermissionModal && (
            <div style={styles.modalOverlay}>
              <div style={styles.modalContent}>
                <div style={styles.modalHeader}>
                  <div style={pendingAction === 'scan' ? styles.modalIconCamera : styles.modalIconLocation}>
                    {pendingAction === 'scan' ? (
                      <FiCamera style={{ color: '#FFFFFF', fontSize: '24px' }} />
                    ) : (
                      <FiMapPin style={{ color: '#FFFFFF', fontSize: '24px' }} />
                    )}
                  </div>
                  <h2 style={styles.modalTitle}>
                    {pendingAction === 'scan' ? 'Camera Access Required' : 'Location Access Required'}
                  </h2>
                  <button 
                    onClick={() => setShowPermissionModal(false)}
                    style={styles.modalCloseButton}
                  >
                    <FiX size={20} />
                  </button>
                </div>
                
                <p style={styles.permissionDescription}>
                  {pendingAction === 'scan' 
                    ? 'To scan QR codes for passenger verification, we need access to your camera. Please allow camera access when prompted.'
                    : 'To start the trip and track your journey, we need access to your location. Please allow location access when prompted.'}
                </p>
                
                <div style={styles.modalButtons}>
                  <button onClick={handleGrantPermission} style={styles.permissionAllowButton}>
                    Allow Access
                  </button>
                  <button onClick={() => setShowPermissionModal(false)} style={styles.cancelModalButton}>
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          
          {/* Passengers Manifest Modal */}
          {showPassengersManifest && currentTripPassengers && (
            <div style={styles.modalOverlay}>
              <div style={styles.modalContentLarge}>
                <div style={styles.modalHeader}>
                  <div style={styles.modalIconPassengers}>
                    <FiUsers style={{ color: '#FFFFFF', fontSize: '24px' }} />
                  </div>
                  <h2 style={styles.modalTitle}>All Passengers</h2>
                  <button 
                    onClick={() => setShowPassengersManifest(false)}
                    style={styles.modalCloseButton}
                  >
                    <FiX size={20} />
                  </button>
                </div>
                
                <div style={styles.passengerStats}>
                  <div style={styles.statCard}>
                    <div style={styles.statIconBoarding}>
                      <FiUser size={20} />
                    </div>
                    <div>
                      <p style={styles.statLabel}>Total Passengers</p>
                      <p style={styles.statValue}>{currentTripPassengers.total_passengers}</p>
                    </div>
                  </div>
                </div>
                
                {currentTripPassengers.passengers.length > 0 ? (
                  <div style={styles.passengerLists}>
                    <div style={styles.passengerSection}>
                      <h3 style={styles.passengerSectionTitle}>
                        <FiUser size={16} />
                        Passengers List ({currentTripPassengers.total_passengers})
                      </h3>
                      <div style={styles.passengerList}>
                        {currentTripPassengers.passengers.map((passenger) => {
                          const displayName = getDisplayName(passenger);
                          const contactInfo = getContactInfo(passenger);
                          return (
                            <div key={passenger.booking_id} style={styles.passengerCard}>
                              <div style={styles.passengerHeader}>
                                <div style={styles.passengerAvatar}>
                                  <FiUser size={16} />
                                </div>
                                <div style={{ flex: 1 }}>
                                  <div style={{ display: 'flex', alignItems: 'center', gap: '8px', flexWrap: 'wrap' as const }}>
                                    <p style={styles.passengerName}>{displayName}</p>
                                    <SeatBadge seatNumber={passenger.seat_number} />
                                  </div>
                                  <p style={styles.passengerId}>ID: {passenger.passenger_id?.slice(0, 8)}...</p>
                                </div>
                              </div>
                              
                              {(contactInfo.phone || contactInfo.email) && (
                                <div style={styles.contactInfo as React.CSSProperties}>
                                  {contactInfo.phone && (
                                    <div style={styles.contactItem as React.CSSProperties}>
                                      <FiPhone size={12} />
                                      <span>{contactInfo.phone}</span>
                                    </div>
                                  )}
                                  {contactInfo.email && (
                                    <div style={styles.contactItem as React.CSSProperties}>
                                      <FiMail size={12} />
                                      <span>{contactInfo.email}</span>
                                    </div>
                                  )}
                                </div>
                              )}
                              
                              {passenger.traveller_relationship_label && (
                                <div style={styles.relationshipLabel as React.CSSProperties}>
                                  {passenger.traveller_relationship_label}
                                </div>
                              )}
                              
                              <div style={styles.passengerDetails}>
                                <div style={styles.passengerStop}>
                                  <FiMapPin size={12} style={{ color: '#10B981' }} />
                                  <span>Pickup: {passenger.pickup_stop?.name || 'N/A'}</span>
                                </div>
                                <div style={styles.passengerStop}>
                                  <FiFlag size={12} style={{ color: '#EF4444' }} />
                                  <span>Drop: {passenger.dropoff_stop?.name || 'N/A'}</span>
                                </div>
                                <div style={styles.passengerFare}>
                                  <FiDollarSign size={12} style={{ color: '#F59E0B' }} />
                                  <span>Fare: ₹{passenger.fare_amount}</span>
                                </div>
                              </div>
                              
                              <div style={styles.passengerStatus}>
                                <span style={styles.statusBadgeBoarding}>
                                  {passenger.status || passenger.booking_status || 'Booked'}
                                </span>
                              </div>
                            </div>
                          );
                        })}
                      </div>
                    </div>
                  </div>
                ) : (
                  <div style={styles.noPassengersMessage}>
                    <FiUsers size={48} style={{ color: isDarkMode ? '#374151' : '#9CA3AF', marginBottom: '16px' }} />
                    <p style={styles.noPassengersText}>No passengers found</p>
                    <p style={styles.noPassengersSubtext}>No bookings for this trip</p>
                  </div>
                )}
                
                <div style={styles.modalButtons}>
                  <button onClick={() => setShowPassengersManifest(false)} style={styles.closeModalButton}>
                    Close
                  </button>
                </div>
              </div>
            </div>
          )}
          
          {/* Passenger Details Modal (Stop-specific) - FIXED: Shows passenger details in popup */}
          {showPassengerModal && passengerDetails && (
            <div style={styles.modalOverlay}>
              <div style={styles.modalContentLarge}>
                <div style={styles.modalHeader}>
                  <div style={styles.modalIconPassengers}>
                    <FiUsers style={{ color: '#FFFFFF', fontSize: '24px' }} />
                  </div>
                  <h2 style={styles.modalTitle}>Passenger Details</h2>
                  <button 
                    onClick={() => setShowPassengerModal(false)}
                    style={styles.modalCloseButton}
                  >
                    <FiX size={20} />
                  </button>
                </div>
                
                <div style={styles.stopInfoSection}>
                  <FiMapPin style={{ color: '#10B981', fontSize: '18px' }} />
                  <span style={styles.stopInfoText}>Stop: {calculatedStops.find(s => s.stop_id === passengerDetails.stop_id)?.name || passengerDetails.stop_id}</span>
                </div>
                
                <div style={styles.passengerStats}>
                  <div style={styles.statCard}>
                    <div style={styles.statIconBoarding}>
                      <FiUserPlus size={20} />
                    </div>
                    <div>
                      <p style={styles.statLabel}>Boarding</p>
                      <p style={styles.statValue}>{passengerDetails.boarding_count}</p>
                    </div>
                  </div>
                  <div style={styles.statCard}>
                    <div style={styles.statIconDropping}>
                      <FiUser size={20} />
                    </div>
                    <div>
                      <p style={styles.statLabel}>Dropping</p>
                      <p style={styles.statValue}>{passengerDetails.drop_count}</p>
                    </div>
                  </div>
                </div>
                
                {(passengerDetails.boarding_passengers.length > 0 || passengerDetails.drop_passengers.length > 0) ? (
                  <div style={styles.passengerLists}>
                    {passengerDetails.boarding_passengers.length > 0 && (
                      <div style={styles.passengerSection}>
                        <h3 style={styles.passengerSectionTitle}>
                          <FiUserPlus size={16} />
                          Boarding Passengers ({passengerDetails.boarding_count})
                        </h3>
                        <div style={styles.passengerList}>
                          {passengerDetails.boarding_passengers.map((passenger) => (
                            <PassengerCard key={passenger.booking_id} passenger={passenger} type="boarding" styles={styles} />
                          ))}
                        </div>
                      </div>
                    )}
                    
                    {passengerDetails.drop_passengers.length > 0 && (
                      <div style={styles.passengerSection}>
                        <h3 style={styles.passengerSectionTitle}>
                          <FiUser size={16} />
                          Dropping Passengers ({passengerDetails.drop_count})
                        </h3>
                        <div style={styles.passengerList}>
                          {passengerDetails.drop_passengers.map((passenger) => (
                            <PassengerCard key={passenger.booking_id} passenger={passenger} type="dropping" styles={styles} />
                          ))}
                        </div>
                      </div>
                    )}
                  </div>
                ) : (
                  <div style={styles.noPassengersMessage}>
                    <FiUsers size={48} style={{ color: isDarkMode ? '#374151' : '#9CA3AF', marginBottom: '16px' }} />
                    <p style={styles.noPassengersText}>No passengers for this stop</p>
                    <p style={styles.noPassengersSubtext}>No boarding or dropping passengers at this location</p>
                  </div>
                )}
                
                <div style={styles.modalButtons}>
                  <button onClick={() => setShowPassengerModal(false)} style={styles.closeModalButton}>
                    Close
                  </button>
                </div>
              </div>
            </div>
          )}
          
          {/* OTP Verification Modal */}
          {showOtpModal && (
            <div style={styles.modalOverlay}>
              <div style={styles.modalContent}>
                <div style={styles.modalHeader}>
                  <div style={styles.modalIconOtp}>
                    <FiKey style={{ color: '#FFFFFF', fontSize: '24px' }} />
                  </div>
                  <h2 style={styles.modalTitle}>Verify Passenger</h2>
                  <button 
                    onClick={() => setShowOtpModal(false)}
                    style={styles.modalCloseButton}
                  >
                    <FiX size={20} />
                  </button>
                </div>
                
                <p style={styles.otpDescription}>
                  Enter the 6-digit OTP provided by the passenger to verify their boarding.
                </p>
                
                <div style={styles.otpInputContainer}>
                  <input
                    type="text"
                    maxLength={6}
                    pattern="[0-9]*"
                    inputMode="numeric"
                    placeholder="••••••"
                    value={otpCode}
                    onChange={(e) => {
                      const value = e.target.value.replace(/[^0-9]/g, '');
                      setOtpCode(value);
                    }}
                    style={styles.otpInput}
                    autoFocus
                  />
                </div>
                
                <div style={styles.modalButtons}>
                  <button 
                    onClick={verifyOtp} 
                    disabled={!otpCode || otpCode.length !== 6 || verifyingOtp} 
                    style={{ 
                      ...styles.otpSubmitButton, 
                      opacity: (!otpCode || otpCode.length !== 6 || verifyingOtp) ? 0.5 : 1 
                    }}
                  >
                    {verifyingOtp ? (
                      <>
                        <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white inline-block mr-2"></div>
                        Verifying...
                      </>
                    ) : (
                      <>
                        <FiUserCheck size={18} />
                        Verify Passenger
                      </>
                    )}
                  </button>
                  <button onClick={() => setShowOtpModal(false)} style={styles.cancelModalButton}>
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          
          {/* Scan Result Toast */}
          {scanResult && (
            <div style={styles.scanResultCard}>
              <div style={{
                ...styles.scanResultContent,
                background: scanResult.error ? (isDarkMode ? '#7F1D1D' : '#FEE2E2') : (isDarkMode ? '#064E3B' : '#D1FAE5')
              }}>
                {scanResult.error ? <FiAlertCircle style={{ color: '#EF4444', fontSize: '24px' }} /> : <FiUserCheck style={{ color: '#10B981', fontSize: '24px' }} />}
                <div>
                  <p style={styles.scanResultTitle}>{scanResult.error ? "Verification Failed" : "Passenger Verified"}</p>
                  <p style={styles.scanResultText}>
                    {scanResult.error ? scanResult.error : 
                     scanResult.seat_number ? `Passenger (Seat ${scanResult.seat_number}) has been successfully verified` : 
                     "Passenger has been successfully verified"}
                  </p>
                </div>
                <button onClick={() => setScanResult(null)} style={styles.scanResultClose}><FiX /></button>
              </div>
            </div>
          )}
          
          {/* QR Scanner */}
          {showScanner && trip && token && hasCameraPermission && (
            <QRScannerComponent
              onClose={() => setShowScanner(false)}
              onScanSuccess={handleScanSuccess}
              tripId={trip.trip_id || trip.id}
              token={token}
            />
          )}
          
          {/* Cancel Trip Modal */}
          {showCancelModal && (
            <div style={styles.modalOverlay}>
              <div style={styles.modalContent}>
                <div style={styles.modalHeader}>
                  <div style={styles.modalIconCancel}><FiX style={{ color: '#FFFFFF', fontSize: '24px' }} /></div>
                  <h2 style={styles.modalTitle}>Cancel Trip</h2>
                  <button 
                    onClick={() => setShowCancelModal(false)}
                    style={styles.modalCloseButton}
                  >
                    <FiX size={20} />
                  </button>
                </div>
                <textarea
                  style={styles.textarea}
                  rows={4}
                  placeholder="Enter reason for cancellation (minimum 100 characters)..."
                  value={cancelReason}
                  onChange={(e) => { setCancelReason(e.target.value); setCancelCharCount(e.target.value.length); }}
                />
                <div style={styles.charCounter}>
                  <span style={{ ...styles.charCountText, color: cancelCharCount >= 100 ? '#10B981' : cancelCharCount > 0 ? '#F59E0B' : '#EF4444' }}>
                    {cancelCharCount} / 100 characters
                  </span>
                  {cancelCharCount >= 100 && <FiCheckCircle style={{ color: '#10B981', fontSize: '14px' }} />}
                </div>
                <div style={styles.modalButtons}>
                  <button 
                    onClick={submitCancelTrip} 
                    disabled={!cancelReason || cancelCharCount < 100 || loading} 
                    style={{ 
                      ...styles.submitButton, 
                      opacity: (!cancelReason || cancelCharCount < 100 || loading) ? 0.5 : 1 
                    }}
                  >
                    {loading ? "Processing..." : "Submit"}
                  </button>
                  <button onClick={() => setShowCancelModal(false)} style={styles.cancelModalButton}>
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          
          {/* Emergency Stop Modal - FIXED: Calls API on submit */}
          {showEmergencyModal && (
            <div style={styles.modalOverlay}>
              <div style={styles.modalContent}>
                <div style={styles.modalHeader}>
                  <div style={styles.modalIconEmergency}>
                    <FiAlertCircle style={{ color: '#FFFFFF', fontSize: '24px' }} />
                  </div>
                  <h2 style={styles.modalTitle}>Emergency Stop</h2>
                  <button 
                    onClick={() => setShowEmergencyModal(false)}
                    style={styles.modalCloseButton}
                  >
                    <FiX size={20} />
                  </button>
                </div>
                <p style={{
                  fontSize: '14px',
                  color: isDarkMode ? '#FCA5A5' : '#991B1B',
                  marginBottom: '16px',
                  padding: '12px',
                  background: isDarkMode ? '#7F1D1D40' : '#FEE2E2',
                  borderRadius: '12px',
                  textAlign: 'center' as const,
                }}>
                  ⚠️ This will immediately end the trip. 
                </p>
                <textarea
                  style={styles.textarea}
                  rows={4}
                  placeholder="Enter reason for emergency stop (minimum 5 characters)..."
                  value={emergencyReason}
                  onChange={(e) => { setEmergencyReason(e.target.value); setEmergencyCharCount(e.target.value.length); }}
                />
                <div style={styles.charCounter}>
                  <span style={{ ...styles.charCountText, color: emergencyCharCount >= 5 ? '#10B981' : emergencyCharCount > 0 ? '#F59E0B' : '#EF4444' }}>
                    {emergencyCharCount} / 5 characters
                  </span>
                  {emergencyCharCount >= 5 && <FiCheckCircle style={{ color: '#10B981', fontSize: '14px' }} />}
                </div>
                <div style={styles.modalButtons}>
                  <button 
                    onClick={submitEmergencyStop} 
                    disabled={!emergencyReason || emergencyCharCount < 5 || loading} 
                    style={{ 
                      ...styles.emergencySubmitButton, 
                      opacity: (!emergencyReason || emergencyCharCount < 5 || loading) ? 0.5 : 1 
                    }}
                  >
                    {loading ? "Processing..." : "Submit Emergency Stop"}
                  </button>
                  <button onClick={() => setShowEmergencyModal(false)} style={styles.cancelModalButton}>
                    Cancel
                  </button>
                </div>
              </div>
            </div>
          )}
          
        </div>
      </IonContent>

      <style>{`
        @keyframes popupFadeIn {
          from { opacity: 0; transform: translate(-50%, -50%) scale(0.9); }
          to { opacity: 1; transform: translate(-50%, -50%) scale(1); }
        }
        
        @keyframes pulse {
          0%, 100% {
            transform: scale(1);
            box-shadow: 0 0 0 0 rgba(245, 158, 11, 0.4);
          }
          50% {
            transform: scale(1.02);
            box-shadow: 0 0 0 8px rgba(245, 158, 11, 0);
          }
        }
        
        @keyframes pulseGreen {
          0%, 100% {
            transform: scale(1);
            box-shadow: 0 0 0 0 rgba(16, 185, 129, 0.4);
          }
          50% {
            transform: scale(1.02);
            box-shadow: 0 0 0 8px rgba(16, 185, 129, 0);
          }
        }
        
        @keyframes slideUp {
          from {
            opacity: 0;
            transform: translateY(20px);
          }
          to {
            opacity: 1;
            transform: translateY(0);
          }
        }
      `}</style>
    </IonPage>
  );
};

// ============================================================
// Styles
// ============================================================

const getStyles = (isDark: boolean, trip: any, nearStopInfo: NearStopInfo) => ({
  container: {
    paddingTop: '80px',
    paddingLeft: '16px',
    paddingRight: '16px',
    paddingBottom: '32px',
    maxWidth: '600px',
    margin: '0 auto',
    minHeight: '100vh',
    background: isDark ? '#000000' : '#F8F9FA'
  },
  wsStatusBar: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    padding: '8px 4px',
    marginBottom: '12px',
  },
  emptyState: {
    textAlign: 'center' as const,
    padding: '60px 20px',
    background: isDark ? '#111111' : '#FFFFFF',
    borderRadius: '24px',
    border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`
  },
  emptyIcon: { fontSize: '64px', color: isDark ? '#374151' : '#9CA3AF', marginBottom: '16px' },
  emptyTitle: { fontSize: '20px', fontWeight: 'bold', color: isDark ? '#FFFFFF' : '#111827', marginBottom: '8px' },
  emptyText: { fontSize: '14px', color: isDark ? '#9CA3AF' : '#6B7280' },
  
  waitingCard: {
    background: isDark 
      ? 'linear-gradient(135deg, #1E293B 0%, #0F172A 100%)'
      : 'linear-gradient(135deg, #F1F5F9 0%, #E2E8F0 100%)',
    borderRadius: '20px',
    padding: '20px',
    marginBottom: '20px',
    border: `1px solid ${isDark ? '#334155' : '#CBD5E1'}`,
  },
  waitingContent: {
    display: 'flex',
    alignItems: 'center',
    gap: '16px',
  },
  waitingTitle: {
    fontSize: '16px',
    fontWeight: '600',
    color: isDark ? '#FFFFFF' : '#1E293B',
    marginBottom: '4px',
  },
  waitingText: {
    fontSize: '13px',
    color: isDark ? '#94A3B8' : '#64748B',
    margin: 0,
  },
  
  stopCardDepartReady: {
    border: `2px solid #8B5CF6`,
    boxShadow: '0 0 0 3px rgba(139, 92, 246, 0.2)',
    background: isDark ? '#4C1D9520' : '#F3E8FF20',
  },
  departReadyBadge: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '4px',
    padding: '2px 8px',
    background: '#8B5CF6',
    borderRadius: '12px',
    fontSize: '9px',
    fontWeight: 'bold',
    color: '#FFFFFF',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px',
  },
  departAllowedBadge: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '4px',
    padding: '2px 8px',
    background: '#8B5CF620',
    color: '#8B5CF6',
    borderRadius: '12px',
    fontSize: '10px',
    fontWeight: '500',
    border: `1px solid #8B5CF640`,
  },
  
  readyToStartCard: {
    background: isDark 
      ? 'linear-gradient(135deg, #064E3B 0%, #022C22 100%)'
      : 'linear-gradient(135deg, #D1FAE5 0%, #A7F3D0 100%)',
    borderRadius: '24px',
    padding: '24px',
    marginBottom: '20px',
    border: `2px solid ${isDark ? '#10B981' : '#059669'}`,
    animation: 'pulseGreen 1.5s ease-in-out infinite',
  },
  readyToStartHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: '16px',
    marginBottom: '20px',
  },
  readyToStartTitle: {
    fontSize: '18px',
    fontWeight: 'bold',
    color: isDark ? '#FFFFFF' : '#064E3B',
    marginBottom: '4px',
  },
  readyToStartText: {
    fontSize: '13px',
    color: isDark ? '#A7F3D0' : '#065F46',
    margin: 0,
  },
  readyToStartButton: {
    width: '100%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: '10px',
    padding: '14px',
    background: '#10B981',
    border: 'none',
    borderRadius: '16px',
    color: '#FFFFFF',
    fontSize: '16px',
    fontWeight: '600',
    cursor: 'pointer',
    transition: 'all 0.3s ease',
    boxShadow: '0 4px 14px rgba(16, 185, 129, 0.4)',
  },
  
  nearStopCard: {
    background: isDark 
      ? 'linear-gradient(135deg, #78350F 0%, #92400E 100%)'
      : 'linear-gradient(135deg, #FEF3C7 0%, #FDE68A 100%)',
    borderRadius: '20px',
    padding: '20px',
    marginBottom: '20px',
    border: `2px solid ${isDark ? '#F59E0B' : '#D97706'}`,
    boxShadow: '0 8px 25px rgba(245, 158, 11, 0.3)',
    animation: 'pulse 2s ease-in-out infinite'
  },
  arrivedStopCard: {
    background: isDark 
      ? 'linear-gradient(135deg, #064E3B 0%, #065F46 100%)'
      : 'linear-gradient(135deg, #D1FAE5 0%, #A7F3D0 100%)',
    borderRadius: '20px',
    padding: '20px',
    marginBottom: '20px',
    border: `2px solid ${isDark ? '#10B981' : '#059669'}`,
    boxShadow: '0 8px 25px rgba(16, 185, 129, 0.3)',
    animation: 'pulseGreen 2s ease-in-out infinite'
  },
  nearStopHeader: {
    display: 'flex',
    gap: '16px',
    marginBottom: '16px'
  },
  nearStopIcon: {
    width: '56px',
    height: '56px',
    borderRadius: '28px',
    background: isDark ? 'rgba(245, 158, 11, 0.2)' : 'rgba(245, 158, 11, 0.2)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center'
  },
  arrivedStopIcon: {
    width: '56px',
    height: '56px',
    borderRadius: '28px',
    background: isDark ? 'rgba(16, 185, 129, 0.2)' : 'rgba(16, 185, 129, 0.2)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center'
  },
  nearStopContent: {
    flex: 1
  },
  nearStopTitle: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '12px',
    fontWeight: '600',
    color: isDark ? '#FDE68A' : '#92400E',
    marginBottom: '8px',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px'
  },
  arrivedStopTitle: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '12px',
    fontWeight: '600',
    color: isDark ? '#A7F3D0' : '#064E3B',
    marginBottom: '8px',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px'
  },
  nearStopName: {
    fontSize: '20px',
    fontWeight: 'bold',
    color: isDark ? '#FFFFFF' : '#78350F',
    marginBottom: '8px'
  },
  arrivedStopName: {
    fontSize: '20px',
    fontWeight: 'bold',
    color: isDark ? '#FFFFFF' : '#064E3B',
    marginBottom: '8px'
  },
  nearStopDistance: {
    fontSize: '14px',
    fontWeight: '500',
    color: isDark ? '#FDE68A' : '#92400E',
    marginBottom: '8px'
  },
  arrivedStopDistance: {
    fontSize: '14px',
    fontWeight: '500',
    color: isDark ? '#A7F3D0' : '#065F46',
    marginBottom: '8px'
  },
  arrivalAlert: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '8px 12px',
    background: isDark ? 'rgba(255,255,255,0.1)' : 'rgba(255,255,255,0.6)',
    borderRadius: '12px',
    fontSize: '13px',
    fontWeight: '500',
    color: isDark ? '#FFFFFF' : '#064E3B',
    marginTop: '8px'
  },
  nearStopFooter: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingTop: '12px',
    borderTop: `1px solid ${isDark ? 'rgba(255,255,255,0.1)' : 'rgba(0,0,0,0.1)'}`
  },
  radiusInfo: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '11px',
    color: isDark ? '#FDE68A' : '#78350F'
  },
  checkingBadge: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '11px',
    padding: '4px 8px',
    background: isDark ? 'rgba(255,255,255,0.1)' : 'rgba(255,255,255,0.4)',
    borderRadius: '12px',
    color: isDark ? '#FFFFFF' : '#78350F'
  },
  tripCard: {
    background: isDark ? '#111111' : '#FFFFFF',
    borderRadius: '24px',
    padding: '20px',
    marginBottom: '20px',
    border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`,
    boxShadow: isDark ? '0 4px 20px rgba(0,0,0,0.3)' : '0 4px 20px rgba(0,0,0,0.05)'
  },
  tripHeader: { display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: '20px', flexWrap: 'wrap' as const, gap: '12px' },
  routeBadge: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '8px' },
  routeIcon: { color: '#10B981', fontSize: '16px' },
  routeName: { fontSize: '16px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  statusBadge: {
    display: 'inline-flex', alignItems: 'center', gap: '6px', padding: '6px 12px', borderRadius: '20px', fontSize: '12px', fontWeight: '500',
    background: trip?.status === 'scheduled' ? (isDark ? '#F59E0B20' : '#FEF3C7') : (isDark ? '#3B82F620' : '#DBEAFE'),
    color: trip?.status === 'scheduled' ? '#F59E0B' : '#3B82F6'
  },
  statusIcon: { fontSize: '12px' },
  scanButton: { display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 20px', background: '#000000', border: 'none', borderRadius: '40px', color: '#FFFFFF', fontSize: '14px', fontWeight: '500', cursor: 'pointer' },
  otpButton: { display: 'flex', alignItems: 'center', gap: '8px', padding: '10px 20px', background: '#3B82F6', border: 'none', borderRadius: '40px', color: '#FFFFFF', fontSize: '14px', fontWeight: '500', cursor: 'pointer' },
  scanIcon: { fontSize: '18px' },
  tripInfo: { display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: '16px', padding: '16px 0', borderTop: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`, borderBottom: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`, marginBottom: '16px' },
  infoItem: { display: 'flex', alignItems: 'center', gap: '10px' },
  infoIcon: { fontSize: '18px', color: isDark ? '#6B7280' : '#9CA3AF' },
  infoLabel: { fontSize: '10px', color: isDark ? '#6B7280' : '#9CA3AF', marginBottom: '2px' },
  infoValue: { fontSize: '13px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  vehicleCard: { background: isDark ? '#0A0A0A' : '#F9FAFB', borderRadius: '16px', padding: '12px', marginBottom: '16px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}` },
  vehicleInner: { display: 'flex', alignItems: 'center', gap: '12px' },
  vehicleLabel: { fontSize: '10px', color: isDark ? '#9CA3AF' : '#6B7280', marginBottom: '2px' },
  vehicleValue: { fontSize: '13px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  scheduledMessage: {
    marginTop: '16px',
    padding: '16px',
    background: isDark ? '#F59E0B10' : '#FEF3C7',
    borderRadius: '16px',
    border: `1px solid ${isDark ? '#F59E0B30' : '#FBBF24'}`,
    display: 'flex',
    alignItems: 'center',
    gap: '12px'
  },
  scheduledTitle: {
    fontSize: '14px',
    fontWeight: '600',
    color: isDark ? '#F59E0B' : '#92400E',
    marginBottom: '4px'
  },
  scheduledText: {
    fontSize: '12px',
    color: isDark ? '#F59E0BCC' : '#B45309',
    margin: 0,
    lineHeight: '1.4'
  },
  totalDurationCard: { background: isDark ? '#0A0A0A' : '#F9FAFB', borderRadius: '16px', padding: '16px', marginBottom: '16px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}` },
  totalDurationInner: { display: 'flex', alignItems: 'center', gap: '12px' },
  totalDurationLabel: { fontSize: '12px', color: isDark ? '#9CA3AF' : '#6B7280', marginBottom: '2px' },
  totalDurationValue: { fontSize: '18px', fontWeight: 'bold', color: isDark ? '#FFFFFF' : '#111827' },
  actualTimesSection: { marginTop: '16px', padding: '16px', background: isDark ? '#0A0A0A' : '#F9FAFB', borderRadius: '16px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`, marginBottom: '16px' },
  actualTimesHeader: { display: 'flex', alignItems: 'center', gap: '8px', marginBottom: '12px' },
  actualIcon: { fontSize: '16px', color: '#10B981' },
  actualTitle: { fontSize: '13px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  actualTimesGrid: { display: 'grid', gridTemplateColumns: 'repeat(2, 1fr)', gap: '16px' },
  actualTimeItem: { flex: 1 },
  actualLabel: { fontSize: '10px', color: isDark ? '#6B7280' : '#9CA3AF', marginBottom: '4px' },
  actualValue: { fontSize: '13px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  actionButtons: { display: 'flex', gap: '12px' },
  startButton: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px', background: '#10B981', border: 'none', borderRadius: '12px', color: '#FFFFFF', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  cancelButton: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px', background: '#EF4444', border: 'none', borderRadius: '12px', color: '#FFFFFF', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  endButton: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px', background: '#EF4444', border: 'none', borderRadius: '12px', color: '#FFFFFF', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  emergencyButton: { flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: '8px', padding: '12px', background: '#F59E0B', border: 'none', borderRadius: '12px', color: '#FFFFFF', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  buttonIcon: { fontSize: '16px' },
  passengersManifestButtonWrapper: {
    marginBottom: '20px'
  },
  viewManifestButton: {
    width: '100%',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: '8px',
    padding: '14px',
    background: '#8B5CF6',
    border: 'none',
    borderRadius: '16px',
    color: '#FFFFFF',
    fontSize: '15px',
    fontWeight: '600',
    cursor: 'pointer',
    transition: 'all 0.2s'
  },
  stopsSection: { background: isDark ? '#111111' : '#FFFFFF', borderRadius: '24px', padding: '20px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}` },
  stopsHeader: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' },
  stopsHeaderLeft: { display: 'flex', alignItems: 'center', gap: '8px' },
  stopsTitle: { fontSize: '18px', fontWeight: 'bold', color: isDark ? '#FFFFFF' : '#111827', margin: 0 },
  stopCount: { fontSize: '12px', color: isDark ? '#9CA3AF' : '#6B7280' },
  stopsList: { display: 'flex', flexDirection: 'column' as const, gap: '16px', maxHeight: '500px', overflowY: 'auto' as const, paddingRight: '8px' },
  stopCard: { background: isDark ? '#0A0A0A' : '#F9FAFB', borderRadius: '16px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`, transition: 'all 0.2s' },
  stopCardNear: { 
    border: `2px solid #F59E0B`,
    boxShadow: '0 0 0 3px rgba(245, 158, 11, 0.2)',
    background: isDark ? '#78350F20' : '#FEF3C720'
  },
  stopCardArrived: { 
    border: `2px solid #10B981`,
    boxShadow: '0 0 0 3px rgba(16, 185, 129, 0.2)',
    background: isDark ? '#064E3B20' : '#D1FAE520'
  },
  stopCardInner: { display: 'flex', alignItems: 'flex-start', gap: '16px', padding: '16px' },
  stopNumberBadge: { flexShrink: 0 },
  stopNumber: { width: '44px', height: '44px', borderRadius: '22px', display: 'flex', alignItems: 'center', justifyContent: 'center', fontSize: '18px', fontWeight: 'bold' },
  stopDetails: { flex: 1 },
  stopName: { fontSize: '16px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827', marginBottom: '10px' },
  approachingBadge: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '4px',
    padding: '2px 8px',
    background: '#F59E0B',
    borderRadius: '12px',
    fontSize: '9px',
    fontWeight: 'bold',
    color: '#FFFFFF',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px'
  },
  arrivedBadgeNew: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: '4px',
    padding: '2px 8px',
    background: '#10B981',
    borderRadius: '12px',
    fontSize: '9px',
    fontWeight: 'bold',
    color: '#FFFFFF',
    textTransform: 'uppercase' as const,
    letterSpacing: '0.5px'
  },
  timeGrid: { display: 'flex', flexWrap: 'wrap' as const, gap: '8px', marginBottom: '10px' },
  timeBadge: { display: 'flex', alignItems: 'center', gap: '6px', padding: '4px 10px', background: isDark ? '#1F1F1F' : '#FFFFFF', borderRadius: '20px', fontSize: '12px' },
  timeDot: { width: '8px', height: '8px', borderRadius: '4px' },
  timeLabel: { fontSize: '11px', color: isDark ? '#9CA3AF' : '#6B7280' },
  timeValue: { fontSize: '12px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  actualStopTimes: { display: 'flex', flexWrap: 'wrap' as const, gap: '8px', marginBottom: '10px' },
  actualTimeBadge: { display: 'flex', alignItems: 'center', gap: '6px', padding: '4px 10px', background: isDark ? '#1F1F1F' : '#FFFFFF', borderRadius: '20px', fontSize: '12px' },
  actualTimeLabel: { fontSize: '11px', color: isDark ? '#9CA3AF' : '#6B7280' },
  actualTimeValue: { fontSize: '12px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  statusBadges: { display: 'flex', flexWrap: 'wrap' as const, gap: '6px', marginBottom: '10px' },
  boardingBadge: { fontSize: '10px', padding: '4px 8px', borderRadius: '12px', background: '#10B98120', color: '#10B981', fontWeight: '500' },
  deboardingBadge: { fontSize: '10px', padding: '4px 8px', borderRadius: '12px', background: '#3B82F620', color: '#3B82F6', fontWeight: '500' },
  arrivedBadge: { fontSize: '10px', padding: '4px 8px', borderRadius: '12px', background: '#3B82F620', color: '#3B82F6', fontWeight: '500' },
  completedBadge: { fontSize: '10px', padding: '4px 8px', borderRadius: '12px', background: '#10B98120', color: '#10B981', fontWeight: '500' },
  stopActionButtons: { display: 'flex', gap: '8px', flexWrap: 'wrap' as const },
  viewPassengersButton: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    padding: '8px 16px',
    background: '#8B5CF6',
    border: 'none',
    borderRadius: '8px',
    color: '#FFFFFF',
    fontSize: '13px',
    fontWeight: '500',
    cursor: 'pointer',
    transition: 'all 0.2s'
  },
  arriveStopButton: { display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 16px', background: '#10B981', border: 'none', borderRadius: '8px', color: '#FFFFFF', fontSize: '13px', fontWeight: '500', cursor: 'pointer' },
  departStopButton: { display: 'flex', alignItems: 'center', gap: '6px', padding: '8px 16px', background: '#3B82F6', border: 'none', borderRadius: '8px', color: '#FFFFFF', fontSize: '13px', fontWeight: '500', cursor: 'pointer' },
  startJourneyButton: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    padding: '8px 16px',
    background: '#10B981',
    border: 'none',
    borderRadius: '8px',
    color: '#FFFFFF',
    fontSize: '13px',
    fontWeight: '500',
    cursor: 'pointer',
    transition: 'all 0.2s'
  },
  completeTripButton: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    padding: '8px 16px',
    background: '#8B5CF6',
    border: 'none',
    borderRadius: '8px',
    color: '#FFFFFF',
    fontSize: '13px',
    fontWeight: '500',
    cursor: 'pointer',
    transition: 'all 0.2s'
  },
  cumulativeBadge: { textAlign: 'right' as const, flexShrink: 0, minWidth: '80px' },
  cumulativeLabel: { fontSize: '10px', color: isDark ? '#9CA3AF' : '#6B7280', marginBottom: '2px' },
  cumulativeValue: { fontSize: '14px', fontWeight: 'bold', color: isDark ? '#FFFFFF' : '#111827' },
  journeySummary: { marginTop: '20px', padding: '16px', background: isDark ? '#0A0A0A' : '#F9FAFB', borderRadius: '16px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}` },
  summaryInner: { display: 'flex', flexDirection: 'column' as const, gap: '12px' },
  summaryContent: { flex: 1 },
  summaryTitle: { fontSize: '14px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827', marginBottom: '4px' },
  summaryText: { fontSize: '12px', color: isDark ? '#9CA3AF' : '#6B7280' },
  summaryTimes: { display: 'flex', gap: '16px', justifyContent: 'flex-end' },
  summaryTimeItem: { textAlign: 'center' as const },
  summaryTimeLabel: { fontSize: '10px', color: isDark ? '#9CA3AF' : '#6B7280', marginBottom: '2px' },
  summaryTimeValue: { fontSize: '14px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827' },
  
  // Permission Modal Styles
  modalIconCamera: {
    width: '48px',
    height: '48px',
    borderRadius: '24px',
    background: '#000000',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center'
  },
  modalIconLocation: {
    width: '48px',
    height: '48px',
    borderRadius: '24px',
    background: '#10B981',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center'
  },
  permissionDescription: {
    fontSize: '14px',
    color: isDark ? '#9CA3AF' : '#6B7280',
    marginBottom: '24px',
    textAlign: 'center' as const,
    lineHeight: '1.5'
  },
  permissionAllowButton: {
    flex: 1,
    padding: '12px',
    background: '#10B981',
    border: 'none',
    borderRadius: '12px',
    color: '#FFFFFF',
    fontSize: '14px',
    fontWeight: '600',
    cursor: 'pointer'
  },
  
  // Passenger Modal Styles
  modalContentLarge: {
    background: isDark ? '#111111' : '#FFFFFF',
    borderRadius: '24px',
    padding: '24px',
    width: '90%',
    maxWidth: '550px',
    maxHeight: '80vh',
    overflowY: 'auto' as const,
    border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`,
    position: 'relative' as const
  },
  modalIconPassengers: {
    width: '48px',
    height: '48px',
    borderRadius: '24px',
    background: '#8B5CF6',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center'
  },
  stopInfoSection: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    padding: '12px',
    background: isDark ? '#0A0A0A' : '#F9FAFB',
    borderRadius: '12px',
    marginBottom: '20px'
  },
  stopInfoText: {
    fontSize: '14px',
    fontWeight: '500',
    color: isDark ? '#FFFFFF' : '#111827'
  },
  passengerStats: {
    display: 'grid',
    gridTemplateColumns: 'repeat(2, 1fr)',
    gap: '12px',
    marginBottom: '24px'
  },
  statCard: {
    display: 'flex',
    alignItems: 'center',
    gap: '12px',
    padding: '16px',
    background: isDark ? '#0A0A0A' : '#F9FAFB',
    borderRadius: '16px',
    border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`
  },
  statIconBoarding: {
    width: '40px',
    height: '40px',
    borderRadius: '20px',
    background: '#10B98120',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: '#10B981'
  },
  statIconDropping: {
    width: '40px',
    height: '40px',
    borderRadius: '20px',
    background: '#EF444420',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: '#EF4444'
  },
  statLabel: {
    fontSize: '11px',
    color: isDark ? '#9CA3AF' : '#6B7280',
    marginBottom: '4px'
  },
  statValue: {
    fontSize: '24px',
    fontWeight: 'bold',
    color: isDark ? '#FFFFFF' : '#111827'
  },
  passengerLists: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '24px'
  },
  passengerSection: {
    marginBottom: '16px'
  },
  passengerSectionTitle: {
    display: 'flex',
    alignItems: 'center',
    gap: '8px',
    fontSize: '16px',
    fontWeight: '600',
    color: isDark ? '#FFFFFF' : '#111827',
    marginBottom: '12px',
    paddingBottom: '8px',
    borderBottom: `2px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`
  },
  passengerList: {
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '12px',
    maxHeight: '300px',
    overflowY: 'auto' as const
  },
  passengerCard: {
    padding: '12px',
    background: isDark ? '#0A0A0A' : '#F9FAFB',
    borderRadius: '12px',
    border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`
  },
  passengerHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: '10px',
    marginBottom: '10px'
  },
  passengerAvatar: {
    width: '32px',
    height: '32px',
    borderRadius: '16px',
    background: isDark ? '#1F1F1F' : '#E5E7EB',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: isDark ? '#9CA3AF' : '#6B7280'
  },
  passengerName: {
    fontSize: '14px',
    fontWeight: '600',
    color: isDark ? '#FFFFFF' : '#111827',
    marginBottom: '2px'
  },
  passengerId: {
    fontSize: '10px',
    color: isDark ? '#9CA3AF' : '#6B7280'
  },
  contactInfo: {
    marginBottom: '10px',
    paddingLeft: '42px',
    display: 'flex',
    flexDirection: 'column' as const,
    gap: '4px'
  },
  contactItem: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '11px',
    color: isDark ? '#D1D5DB' : '#4B5563'
  },
  relationshipLabel: {
    display: 'inline-block',
    padding: '2px 8px',
    background: '#8B5CF620',
    color: '#8B5CF6',
    borderRadius: '12px',
    fontSize: '10px',
    fontWeight: '500',
    marginLeft: '42px',
    marginBottom: '8px'
  },
  passengerDetails: {
    marginBottom: '10px',
    paddingLeft: '42px'
  },
  passengerStop: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '12px',
    color: isDark ? '#D1D5DB' : '#4B5563',
    marginBottom: '6px'
  },
  passengerFare: {
    display: 'flex',
    alignItems: 'center',
    gap: '6px',
    fontSize: '12px',
    fontWeight: '600',
    color: '#F59E0B',
    marginTop: '6px'
  },
  passengerStatus: {
    paddingLeft: '42px'
  },
  statusBadgeBoarding: {
    display: 'inline-block',
    padding: '4px 8px',
    background: '#10B98120',
    color: '#10B981',
    borderRadius: '8px',
    fontSize: '10px',
    fontWeight: '600',
    textTransform: 'uppercase' as const
  },
  statusBadgeDropping: {
    display: 'inline-block',
    padding: '4px 8px',
    background: '#EF444420',
    color: '#EF4444',
    borderRadius: '8px',
    fontSize: '10px',
    fontWeight: '600',
    textTransform: 'uppercase' as const
  },
  noPassengersMessage: {
    textAlign: 'center' as const,
    padding: '40px 20px',
    background: isDark ? '#0A0A0A' : '#F9FAFB',
    borderRadius: '16px',
    marginBottom: '20px'
  },
  noPassengersText: {
    fontSize: '16px',
    fontWeight: '600',
    color: isDark ? '#FFFFFF' : '#111827',
    marginBottom: '8px'
  },
  noPassengersSubtext: {
    fontSize: '12px',
    color: isDark ? '#9CA3AF' : '#6B7280'
  },
  closeModalButton: {
    flex: 1,
    padding: '12px',
    background: '#8B5CF6',
    border: 'none',
    borderRadius: '12px',
    color: '#FFFFFF',
    fontSize: '14px',
    fontWeight: '600',
    cursor: 'pointer'
  },
  
  scanResultCard: { position: 'fixed' as const, bottom: '20px', left: '16px', right: '16px', zIndex: 100, animation: 'slideUp 0.3s ease-out' },
  scanResultContent: { display: 'flex', alignItems: 'center', gap: '12px', padding: '16px', borderRadius: '16px', boxShadow: '0 8px 25px rgba(0,0,0,0.2)' },
  scanResultTitle: { fontSize: '14px', fontWeight: '600', color: isDark ? '#FFFFFF' : '#111827', marginBottom: '2px' },
  scanResultText: { fontSize: '12px', color: isDark ? '#9CA3AF' : '#6B7280', margin: 0 },
  scanResultClose: { background: 'transparent', border: 'none', cursor: 'pointer', marginLeft: 'auto', color: isDark ? '#9CA3AF' : '#6B7280' },
  modalOverlay: { position: 'fixed' as const, inset: 0, background: 'rgba(0,0,0,0.8)', display: 'flex', alignItems: 'center', justifyContent: 'center', zIndex: 1000, backdropFilter: 'blur(8px)' },
  modalContent: { background: isDark ? '#111111' : '#FFFFFF', borderRadius: '24px', padding: '24px', width: '90%', maxWidth: '450px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`, position: 'relative' as const },
  modalHeader: { display: 'flex', alignItems: 'center', gap: '12px', marginBottom: '20px' },
  modalIconCancel: { width: '48px', height: '48px', borderRadius: '24px', background: '#EF4444', display: 'flex', alignItems: 'center', justifyContent: 'center' },
  modalIconEmergency: { width: '48px', height: '48px', borderRadius: '24px', background: '#F59E0B', display: 'flex', alignItems: 'center', justifyContent: 'center' },
  modalIconOtp: { width: '48px', height: '48px', borderRadius: '24px', background: '#3B82F6', display: 'flex', alignItems: 'center', justifyContent: 'center' },
  modalCloseButton: {
    position: 'absolute' as const,
    top: '20px',
    right: '20px',
    background: 'transparent',
    border: 'none',
    cursor: 'pointer',
    color: isDark ? '#9CA3AF' : '#6B7280',
    padding: '4px'
  },
  modalTitle: { fontSize: '22px', fontWeight: 'bold', color: isDark ? '#FFFFFF' : '#111827', margin: 0 },
  otpDescription: {
    fontSize: '14px',
    color: isDark ? '#9CA3AF' : '#6B7280',
    marginBottom: '24px',
    textAlign: 'center' as const
  },
  otpInputContainer: {
    marginBottom: '24px',
    display: 'flex',
    justifyContent: 'center'
  },
  otpInput: {
    width: '200px',
    padding: '16px',
    fontSize: '32px',
    fontWeight: 'bold',
    textAlign: 'center' as const,
    letterSpacing: '8px',
    borderRadius: '16px',
    border: `2px solid ${isDark ? '#3B82F6' : '#3B82F6'}`,
    background: isDark ? '#0A0A0A' : '#FFFFFF',
    color: isDark ? '#FFFFFF' : '#111827',
    outline: 'none',
    fontFamily: 'monospace'
  },
  otpSubmitButton: {
    flex: 1,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    gap: '8px',
    padding: '12px',
    background: '#3B82F6',
    border: 'none',
    borderRadius: '12px',
    color: '#FFFFFF',
    fontSize: '14px',
    fontWeight: '600',
    cursor: 'pointer'
  },
  textarea: { width: '100%', padding: '14px', borderRadius: '12px', border: `1px solid ${isDark ? '#1F1F1F' : '#E5E7EB'}`, background: isDark ? '#0A0A0A' : '#F9FAFB', color: isDark ? '#FFFFFF' : '#111827', fontSize: '14px', resize: 'vertical' as const, marginBottom: '8px', fontFamily: 'inherit' },
  charCounter: { display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: '20px' },
  charCountText: { fontSize: '12px', fontWeight: '500' },
  modalButtons: { display: 'flex', gap: '12px' },
  submitButton: { flex: 1, padding: '12px', background: '#EF4444', border: 'none', borderRadius: '12px', color: '#FFFFFF', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  emergencySubmitButton: { flex: 1, padding: '12px', background: '#F59E0B', border: 'none', borderRadius: '12px', color: '#FFFFFF', fontSize: '14px', fontWeight: '600', cursor: 'pointer' },
  cancelModalButton: { flex: 1, padding: '12px', background: isDark ? '#1F1F1F' : '#F3F4F6', border: 'none', borderRadius: '12px', color: isDark ? '#FFFFFF' : '#111827', fontSize: '14px', fontWeight: '600', cursor: 'pointer' }
});

export default CurrentTrip;
```
